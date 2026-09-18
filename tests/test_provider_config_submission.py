"""Real HTTP admission -> durable jobs/queue -> independent core worker.

Only auth, billing and supplier transport are test doubles. No paid generation.
The provider version is NOT inserted by the test: the real admission path owns it.
"""
import base64
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import ExitStack, closing
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
from content_domains import provider_config as pc

WORKER = r'''
import hashlib,json,os,sys
from unittest.mock import Mock
from content_domains import core,image
core.JOB_DB=sys.argv[1]
core.HANDLERS={'image': image.gen_image}
core._domains=lambda:(Mock(),Mock(),Mock())
core._short_drama_domain=lambda:Mock()
core._start_job_heartbeat=lambda jid:None
image.public_url=lambda name,mime:'https://output.invalid/'+name
calls=[]
def supplier(path,body,ct,base,key,proxy):
    calls.append({'path':path,'base':base,'key_hash':hashlib.sha256(key.encode()).hexdigest()})
    return {'data':[{'url':'https://output.invalid/result.png'}]}
image._post=supplier
image._seedream_fetch=lambda url:b'test-image-bytes'
core.run_job(int(sys.argv[2]))
print('EVIDENCE:'+json.dumps(calls))
'''


class ProviderSubmissionTests(unittest.TestCase):
    config_store = 'sqlite'
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {
            'ADMIN_DB': str(self.tmp / 'admin.db'),
            'CONTENT_OUT': str(self.tmp / 'out'),
            'HQ_ADMIN_CONFIG_STORE': self.config_store, 'HQ_CHANNEL_STORE': 'sqlite',
            'HQ_PROVIDER_CONFIG_WIRING': 'image.seedream',
            'ARK_API_KEY': 'test-only-key-A',
            'ARK_BASE': 'https://ark.cn-beijing.volces.com/api/v3',
            'HQ_PROVIDER_KEYS_MASTER_KEY': base64.urlsafe_b64encode(b'0'*32).decode(),
        }))
        self.tmp.joinpath('out').mkdir()
        from content_domains import core, image, video, points, channel_manager, channel_parameters, upstream_guard
        self.core, self.image = core, image
        pc.invalidate()
        self.addCleanup(pc.invalidate)
        self.db = str(self.tmp / 'jobs.db')
        with closing(sqlite3.connect(self.db)) as c:
            c.execute('''CREATE TABLE jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT,username TEXT,cost INTEGER,status TEXT DEFAULT 'pending',
                payload TEXT,result TEXT,error TEXT,created_at INTEGER,updated_at INTEGER,
                deleted INTEGER DEFAULT 0,refunded INTEGER DEFAULT 0,owner TEXT,
                submission_key TEXT)''')
            c.commit()
        self.charges = []
        fake_points = Mock()
        fake_points.AuthPointsError = points.AuthPointsError
        fake_points.cost_of.return_value = 10
        fake_points.deduct_points.side_effect = lambda *a, **kw: (self.charges.append(a), 990)[1]
        self.q = queue.Queue()
        for obj, name, val in [
            (core, 'JOB_DB', self.db),
            (core, 'verify', lambda token: {'username':'fixture-user','must_change':False}),
            (core, '_domains', lambda: (Mock(), fake_points, video)),
            (core, 'HANDLERS', {'image': image.gen_image}),
            (core, '_image_job_queue', self.q), (core, '_queued_job_ids', set()),
            (core.feature_flags, 'require_enabled', lambda *a: None),
            (core.miniprogram_security, 'check_payload', lambda *a: None),
            (upstream_guard, 'exhausted_reason', lambda *a: ''),
            (channel_manager, 'capture', lambda kind,payload,**kw: dict(payload)),
            (channel_parameters, 'quote', lambda *a,**kw: None),
        ]:
            self.stack.enter_context(patch.object(obj,name,val))
        self.server = ThreadingHTTPServer(('127.0.0.1',0),core.H)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def submit(self, idem):
        body = {'provider':'seedream','prompt':'test picture','quality':'std','count':1,
                '_provider_config':{'target_id':'image.seedream','version':999999}}
        req = urllib.request.Request('http://127.0.0.1:%s/api/gen/image'%self.server.server_port,
            data=json.dumps(body).encode(),headers={'Content-Type':'application/json',
            'Authorization':'Bearer fixture','Idempotency-Key':'fixture-'+idem})
        try:
            with urllib.request.urlopen(req,timeout=15) as r: return r.status,json.load(r)
        except urllib.error.HTTPError as e: return e.code,json.load(e)

    def payload(self, jid):
        with closing(sqlite3.connect(self.db)) as c:
            return json.loads(c.execute('SELECT payload FROM jobs WHERE id=?',(jid,)).fetchone()[0])

    def publish_b(self):
        baseline = pc.active_version('image.seedream')['seq']
        draft = pc.save_draft('image.seedream',url='https://ark.cn-beijing.volces.com/api/v3/alternate',
                              secret='test-only-key-B',actor='fixture')
        pc.validate_draft('image.seedream',draft['seq'],actor='fixture',
            probe=lambda *a:{'connection':{'ok':True},'auth':{'ok':True}})
        published = pc.publish('image.seedream',draft['seq'],expected_seq=baseline,
                              op_id='fixture-publish',actor='fixture')
        return baseline,published['seq']

    def work(self,jid,key,url):
        import hashlib
        env=dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT/'server'),str(ROOT),os.environ.get('PYTHONPATH','')]))
        result=subprocess.run([sys.executable,'-c',WORKER,self.db,str(jid)],
            env=env,capture_output=True,text=True,timeout=45)
        self.assertEqual(result.returncode,0,result.stderr[-2000:])
        calls=json.loads(next(l[9:] for l in result.stdout.splitlines() if l.startswith('EVIDENCE:')))
        self.assertEqual(calls,[{'path':'/images/generations','base':url,
            'key_hash':hashlib.sha256(key.encode()).hexdigest()}])
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT status FROM jobs WHERE id=?',(jid,)).fetchone()[0],'done')

    def test_http_queue_worker_publish_rollback_and_switch_off(self):
        status,a=self.submit('A');self.assertEqual(status,200,a)
        va=self.payload(a['job_id'])['_provider_config']['version']
        self.assertNotEqual(va,999999)
        baseline,vb=self.publish_b();self.assertEqual(va,baseline)
        status,b=self.submit('B');self.assertEqual(status,200,b)
        self.assertEqual(self.payload(b['job_id'])['_provider_config']['version'],vb)
        status,replay=self.submit('A');self.assertEqual(status,200,replay)
        self.assertEqual(replay['job_id'],a['job_id']);self.assertEqual(len(self.charges),2)
        pc.rollback('image.seedream',expected_seq=vb,op_id='fixture-rollback',actor='fixture',to_seq=va)
        status,c=self.submit('C');self.assertEqual(status,200,c)
        self.assertEqual(self.q.qsize(),3)
        os.environ[pc.WIRING_ENV]=''
        os.environ['ARK_API_KEY']='changed-environment-not-used'
        # Drain the real admission queue; independent worker reads the persisted job.
        expected={a['job_id']:('test-only-key-A',os.environ['ARK_BASE']),
                  b['job_id']:('test-only-key-B',os.environ['ARK_BASE']+'/alternate'),
                  c['job_id']:('test-only-key-A',os.environ['ARK_BASE'])}
        while not self.q.empty():
            queued=self.q.get_nowait()
            jid=queued if isinstance(queued,int) else queued[-1]
            self.work(jid,*expected[jid])
        self.assertTrue(pc.runtime_instances('image.seedream'), 'pinned workers must report loaded versions')

    def test_pin_failure_has_no_charge_no_job_and_retry_is_allowed(self):
        with patch.object(pc,'pin_payload',side_effect=pc.ProviderConfigUnavailable('fixture failure')):
            status,data=self.submit('retry-me')
        self.assertEqual(status,503,data);self.assertEqual(self.charges,[])
        self.assertEqual(self.q.qsize(),0)
        with closing(sqlite3.connect(self.db)) as c:self.assertEqual(c.execute('SELECT count(*) FROM jobs').fetchone()[0],0)
        status,data=self.submit('retry-me');self.assertEqual(status,200,data)

    def test_switch_off_and_other_routes_do_not_pin(self):
        pc.ensure_baseline_version('image.seedream','fixture')
        self.publish_b()
        os.environ[pc.WIRING_ENV]=''
        status,data=self.submit('off');self.assertEqual(status,200,data)
        self.assertNotIn('_provider_config',self.payload(data['job_id']))
        os.environ[pc.WIRING_ENV]='image.seedream'
        for payload in ({'provider':'banana'},{'provider':'openai'},
                        {'provider':'seedream','_channel_binding':{'sealed':True}}):
            with patch.object(pc,'pin_payload') as pin:
                clean=pc.prepare_job_payload('image',payload)
                pin.assert_not_called();self.assertNotIn('_provider_config',clean)

    def test_batch_pin_failure_precedes_all_charges(self):
        from content_domains import jobs_store
        with patch.object(pc,'prepare_job_payload',side_effect=[{},RuntimeError('fixture')]):
            with self.assertRaises(jobs_store.PaidJobDeductError):
                jobs_store.create_paid_jobs(self.core.jdb,Mock(),Mock(),'image','fixture',
                    [(10,{'provider':'seedream'}),(10,{'provider':'seedream'})],'content',
                    before_charge=lambda:self.fail('must not start charge'))
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM jobs').fetchone()[0],0)

    def test_digital_human_child_http_locks_before_durable_charge(self):
        from content_domains import digital_human_oneclick, video, matrix_template_submission
        from tests.test_matrix_template_submission import FakePoints
        points=FakePoints();points.cost_of=lambda *a:10
        with patch.object(self.core,'_domains',lambda:(Mock(),points,video)), patch.object(
                digital_human_oneclick,'verify_child_submission_with_record',
                side_effect=lambda body,*a:(body,{'fixture_consent':True})):
            with patch.object(pc,'pin_payload',side_effect=RuntimeError('unavailable')):
                status,failed=self.submit('child-fail')
            self.assertEqual(status,503,failed);self.assertEqual(points.deductions,[])
            status,a=self.submit('child-A');self.assertEqual(status,200,a)
            va=self.payload(a['job_id'])['_provider_config']['version']
            _,vb=self.publish_b()
            # Replaying the same durable attempt must never re-pin or re-charge.
            attempt=matrix_template_submission.get(self.core.jdb,'fixture-user','/api/gen/image','fixture-child-A')
            with patch.object(pc,'pin_payload',side_effect=AssertionError('re-pinned')):
                recovered=matrix_template_submission.recover(self.core.jdb,points,'fixture-user',
                    '/api/gen/image','fixture-child-A',body=attempt['input'],cost=10,kind='image')
            self.assertEqual(recovered['job_id'],a['job_id'])
            self.assertEqual(recovered['execution']['_provider_config']['version'],va)
            status,b=self.submit('child-B');self.assertEqual(status,200,b)
            self.assertEqual(self.payload(b['job_id'])['_provider_config']['version'],vb)
            self.assertEqual(len(points.deductions),2)
        self.work(a['job_id'],'test-only-key-A',os.environ['ARK_BASE'])
        self.work(b['job_id'],'test-only-key-B',os.environ['ARK_BASE']+'/alternate')

if __name__=='__main__':unittest.main()
