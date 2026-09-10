import base64
import io
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from server.content_domains import channel_manager as cm, channel_runtime as runtime


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'HQ_CHANNEL_DB':self.tmp.name+'/channels.db',
            'HQ_OBSERVABILITY_DB':self.tmp.name+'/trace.db','HQ_PROVIDER_KEYS_MASTER_KEY':base64.urlsafe_b64encode(b'a'*32).decode()})
        self.env.start(); self.addCleanup(self.env.stop)
        self.body = dict(name='测试渠道',adapter='openai_image',model='test-model',base_url='http://127.0.0.1:9999/v1',
                         secret='private-secret',enabled=True,fixture={'prompt':'test'},daily_limit=2,test_cost=1,daily_budget=2)
        self.ch = cm.save('admin',self.body)

    def mapping(self):
        return cm.save_mapping('admin',dict(kind='image',front='front-model',channel=self.ch['id'],enabled=True))

    def test_vault_and_public_redaction(self):
        self.assertNotIn(b'private-secret',Path(self.tmp.name+'/channels.db').read_bytes())
        self.assertNotIn('private-secret',json.dumps(cm.overview()))
        self.assertEqual(cm.version(self.ch['id'],1,True)['secret'],'private-secret')

    def test_supplier_classification_is_versioned_and_validated(self):
        changed=cm.save('admin',dict(self.body,**self.ch,supplier='中转供应商',connection_type='relay'))
        current=cm.version(changed['id'])
        self.assertEqual(current['supplier'],'中转供应商')
        self.assertEqual(current['connection_type'],'relay')
        self.assertNotIn('supplier', {k:v for k,v in cm.version(self.ch['id'],1).items() if v})
        with self.assertRaises(ValueError):
            cm.save('admin',dict(self.body,connection_type='fake'))

    def test_protocol_change_cannot_break_enabled_mappings(self):
        self.mapping()
        with self.assertRaisesRegex(ValueError,'不兼容'):
            cm.save('admin',dict(self.body,**self.ch,adapter='xai_video'))
        self.assertEqual(cm.version(self.ch['id'])['adapter'],'openai_image')

    def test_frequent_connection_checks_do_not_displace_auth_or_generation(self):
        with closing(cm.db()) as c:
            for i,kind in enumerate(['full','auth','connection','connection','connection','connection']):
                c.execute('INSERT INTO runs(id,channel,version,kind,state,started,updated) VALUES(?,?,?,?,?,?,?)',
                          (str(i),self.ch['id'],1,kind,'passed',i+1,i+1))
            c.commit()
        checks=cm.overview()['items'][0]['checks']
        self.assertEqual({r['kind'] for r in checks},{'connection','auth','full'})
        self.assertEqual(next(r for r in checks if r['kind']=='connection')['updated'],6)

    def test_workspace_audit_excludes_secrets_and_unrelated_operations(self):
        import server.admin_api as admin
        with closing(cm.db()) as c:
            c.execute('CREATE TABLE admin_audit(id INTEGER PRIMARY KEY,actor TEXT,action TEXT,target TEXT,detail TEXT,created_at INTEGER)')
            c.executemany('INSERT INTO admin_audit VALUES(?,?,?,?,?,?)',[
                (1,'admin','provider_key.add','key-id','private-secret',1),
                (2,'admin','inspiration.publish','case-id','private-other',2)])
            c.commit()
        with patch.object(admin,'db',cm.db):
            result=admin.channel_workspace_overview()
        self.assertEqual(len(result['legacy_events']),1)
        self.assertEqual(result['legacy_events'][0]['action'],'provider_key.add')
        self.assertNotIn('private-secret',json.dumps(result))
        self.assertNotIn('detail',result['legacy_events'][0])

    def test_snapshot_and_disable(self):
        self.mapping()
        old = cm.capture('image',{'model':'front-model','prompt':'hello'})
        cm.save('admin',dict(self.body,**self.ch,model='new-model',enabled=False))
        self.assertEqual(cm.version(old['_channel_binding']['id'],old['_channel_binding']['version'])['model'],'test-model')
        with self.assertRaisesRegex(ValueError,'停用'):
            cm.capture('image',{'model':'front-model','prompt':'hello'})
        with self.assertRaisesRegex(ValueError,'刷新'):
            cm.save('admin',dict(self.body,**self.ch))

    def test_video_validation_retains_managed_snapshot(self):
        from server.content_domains import feature_flags, video
        binding = {'id': 'managed-video', 'version': 3, 'front': 'grok'}
        captured = {'channel': 'grok', 'prompt': 'demo',
                    '_channel_binding': binding}
        with patch.object(cm, 'capture', return_value=captured), \
                patch.object(feature_flags, 'require_enabled'):
            result = video.validate_xiaole_video_payload(
                {'channel': 'grok', 'prompt': 'demo'})
        self.assertEqual(binding, result['_channel_binding'])
        self.assertEqual('generate', result['operation'])

    def test_untrusted_binding_removed_and_incompatible_rejected(self):
        self.assertNotIn('_channel_binding',cm.capture('copy',{'_channel_binding':self.ch}))
        self.mapping()
        with self.assertRaisesRegex(ValueError,'文生图'):
            cm.capture('image',{'model':'front-model','prompt':'hello','image':'abc'})
        with self.assertRaises(ValueError):
            cm.save_mapping('admin',dict(kind='xiaole_video',front='grok',channel=self.ch['id']))

    def test_budget_reserved_even_when_unknown_and_task_resume_is_idempotent(self):
        rid=cm.reserve(self.ch['id'],'full');cm.finish(rid,'unknown','unknown')
        cm.reserve(self.ch['id'],'full')
        with self.assertRaisesRegex(ValueError,'预算'):
            cm.reserve(self.ch['id'],'full')
        task_rid = cm.reserve(self.ch['id'],'task','77')
        self.assertEqual(task_rid, cm.reserve(self.ch['id'],'task','77'))
        cm.finish(task_rid, 'running', 'submitted')
        with self.assertRaisesRegex(ValueError,'重复'):
            cm.reserve(self.ch['id'],'task','77')

    def test_private_provider_and_proxy_addresses_are_rejected(self):
        for changes in (
            {'base_url':'https://127.0.0.1/v1'},
            {'proxy':'http://169.254.169.254:8080'},
            {'proxy':'https://proxy.example:8443'},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                cm.save('admin', dict(self.body, **self.ch, **changes))

    def test_task_recovery_state_is_durable_and_unreadable_is_conservative(self):
        rid = cm.reserve(self.ch['id'], 'task', '88')
        self.assertEqual('queued', cm.task_recovery_state('88'))
        cm.finish(rid, 'unknown', 'uncertain')
        self.assertEqual('unknown', cm.task_recovery_state('88'))
        with patch.object(cm, 'db', side_effect=OSError('disk unavailable')):
            self.assertEqual('unavailable', cm.task_recovery_state('88'))

    def test_interrupted_running_task_becomes_unknown_once(self):
        rid = cm.reserve(self.ch['id'], 'task', '89')
        cm.finish(rid, 'running', 'provider request started', 'provider-89')
        self.assertEqual('unknown', cm.mark_interrupted_task_unknown(
            '89', 'worker restarted'))
        self.assertEqual('unknown', cm.mark_interrupted_task_unknown(
            '89', 'second recovery pass'))
        evidence = cm.task_evidence('89')
        self.assertEqual('unknown', evidence['state'])
        self.assertEqual('provider-89', evidence['provider_id'])

    def test_managed_evidence_searches_provider_order_and_actual_model(self):
        rid = cm.reserve(self.ch['id'], 'task', '91')
        cm.finish(rid, 'running', 'accepted', 'provider-order-xyz')
        self.assertEqual({'91'}, cm.search_task_ids('provider-order-xyz'))
        self.assertEqual({'91'}, cm.search_task_ids('test-model'))

    def test_concurrent_budget_is_atomic(self):
        def reserve():
            try: return cm.reserve(self.ch['id'],'full')
            except ValueError: return None
        with ThreadPoolExecutor(max_workers=5) as pool:
            results=list(pool.map(lambda _:reserve(),range(5)))
        self.assertEqual(len([x for x in results if x]),2)

    def test_health_separates_connection_and_expiry_and_version(self):
        rid=cm.reserve(self.ch['id'],'connection');cm.finish(rid,'passed','reachable')
        self.assertEqual(cm.overview()['items'][0]['health'],'未验证')
        rid=cm.reserve(self.ch['id'],'full');cm.finish(rid,'passed','artifact checked')
        self.assertEqual(cm.overview()['items'][0]['health'],'成品核验通过')
        with closing(cm.db()) as c:
            c.execute('UPDATE runs SET updated=0 WHERE id=?',(rid,));c.commit()
        self.assertEqual(cm.overview()['items'][0]['health'],'验证已过期')

    def test_rollback_new_version_and_keep_old_materials(self):
        second=cm.save('admin',dict(self.body,**self.ch,model='changed'))
        third=cm.rollback('admin',dict(id=self.ch['id'],target_version=1,enabled=True))
        self.assertEqual(third['version'],3)
        self.assertEqual(cm.version(self.ch['id'])['model'],'test-model')

    def test_scheduler_claims_once(self):
        cm.save('admin',dict(self.body,**self.ch,monitor=True,daily_test=True))
        with closing(cm.db()) as c:
            c.execute('UPDATE schedule SET light_due=0,full_due=0');c.commit()
        with patch.object(runtime,'start_test') as start:
            runtime.monitor_cycle();runtime.monitor_cycle()
        self.assertEqual(start.call_count,2)

    def test_unknown_submission_no_retry(self):
        rid=cm.reserve(self.ch['id'],'full')
        with patch.object(runtime,'request',side_effect=runtime.OutcomeUnknown('unknown')) as request:
            with self.assertRaises(RuntimeError): runtime.execute(rid)
        self.assertEqual(request.call_count,1)
        self.assertEqual(cm.overview()['runs'][0]['state'],'unknown')

    def test_local_http_full_generation_checks_real_image(self):
        from PIL import Image
        image=io.BytesIO();Image.new('RGB',(16,16),'red').save(image,'PNG')
        calls=[]
        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                calls.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                raw=json.dumps({'data':[{'b64_json':base64.b64encode(image.getvalue()).decode()}]}).encode()
                self.send_response(200);self.end_headers();self.wfile.write(raw)
            def log_message(self,*args): pass
        server=ThreadingHTTPServer(('127.0.0.1',0),H)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        cm.save('admin',dict(self.body,**self.ch,base_url='http://127.0.0.1:%s/v1'%server.server_port))
        rid=cm.reserve(self.ch['id'],'full')
        core=types.ModuleType('server.content_domains.core');core.OUT_DIR=Path(self.tmp.name);core.public_url=lambda f,t:'local/'+f
        with patch.dict(sys.modules,{'server.content_domains.core':core}),patch('server.content_domains.core',core,create=True):
            result=runtime.execute(rid)
        self.assertTrue((Path(self.tmp.name)/result['file']).exists())
        self.assertEqual(calls[0]['model'],'test-model')
        self.assertEqual(cm.overview()['runs'][0]['state'],'passed')

    def test_video_protocols_keep_order_id_and_real_model(self):
        for adapter,model,responses in [
            ('minimax_h3','MiniMax-H3',[{'task_id':'supplier-123'},{'task':{'status':'succeeded','content':{'url':'https://example.com/video.mp4'}}}]),
            ('xai_video','grok-imagine-video',[{'request_id':'supplier-123'},{'status':'done','video':{'url':'https://example.com/video.mp4'}}]),
        ]:
            with self.subTest(adapter=adapter):
                ch=cm.save('admin',dict(self.body,adapter=adapter,model=model))
                rid=cm.reserve(ch['id'],'full')
                core=types.ModuleType('server.content_domains.core');core.OUT_DIR=Path(self.tmp.name);core.public_url=lambda f,t:'local/'+f
                probe=types.SimpleNamespace(stdout=b'{"streams":[{"codec_type":"video","width":16,"height":16,"nb_read_frames":"10"}]}',stderr=b'')
                with patch.dict(sys.modules,{'server.content_domains.core':core}),patch('server.content_domains.core',core,create=True),patch.object(runtime,'request',side_effect=responses) as request,patch.object(runtime,'_download',return_value=b'video'),patch.object(runtime.time,'sleep'),patch('subprocess.run',return_value=probe):
                    result=runtime.execute(rid)
                self.assertEqual(result['request_id'],'supplier-123')
                self.assertEqual(request.call_args_list[0].args[3]['model'],model)
                run=next(r for r in cm.overview()['runs'] if r['id']==rid)
                self.assertEqual(run['provider_id'],'supplier-123')
                self.assertEqual(run['state'],'passed')

    def test_notification_config_delivers_only_to_local_sink(self):
        from server.content_domains import runtime_observability as obs
        received=[]
        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(204);self.end_headers()
            def log_message(self,*args): pass
        server=ThreadingHTTPServer(('127.0.0.1',0),H)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        cm.save_notifications('admin',{'enabled':True,'endpoint':'http://127.0.0.1:%d/notify'%server.server_port})
        obs.enqueue('channel.failed',self.ch['id'],123)
        obs.dispatch()
        self.assertEqual(received[0]['event'],'channel.failed')
        self.assertNotIn('secret',json.dumps(received))
        self.assertEqual(cm.notification_settings()['delivery']['sent'],1)

    def test_repeated_executor_cannot_overwrite_claimed_run(self):
        rid=cm.reserve(self.ch['id'],'full')
        cm.finish(rid,'running','provider executing')
        with patch.object(runtime,'generate') as generate:
            self.assertIsNone(runtime.execute(rid))
        self.assertFalse(generate.called)
        self.assertEqual(cm.overview()['runs'][0]['state'],'running')

    def test_concurrent_failures_emit_one_durable_incident(self):
        from server.content_domains import runtime_observability as obs
        rows=[{'id':str(i),'channel':self.ch['id'],'kind':'task','started':i} for i in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda row:runtime._notify(row,'failed'),rows))
        with closing(obs.database()) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM alert_outbox').fetchone()[0],1)
        runtime._notify(rows[0],'passed')
        with closing(obs.database()) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM alert_outbox').fetchone()[0],2)


if __name__=='__main__': unittest.main()
