import base64
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))

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

    def job_db(self):
        path = self.tmp.name + '/jobs.db'
        connection = sqlite3.connect(path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("""CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,username TEXT,cost INTEGER,
            status TEXT DEFAULT 'pending',payload TEXT,result TEXT,error TEXT,
            created_at INTEGER,updated_at INTEGER,owner TEXT,refunded INTEGER DEFAULT 0)""")
        connection.commit()
        return connection

    def test_vault_and_public_redaction(self):
        self.assertNotIn(b'private-secret',Path(self.tmp.name+'/channels.db').read_bytes())
        self.assertNotIn('private-secret',json.dumps(cm.overview()))
        self.assertEqual(cm.version(self.ch['id'],1,True)['secret'],'private-secret')

    def test_schema_upgrade_is_additive_and_idempotent(self):
        legacy_path = self.tmp.name + '/legacy-channels.db'
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.execute('CREATE TABLE settings(id INTEGER PRIMARY KEY,value TEXT)')
            connection.execute("INSERT INTO settings VALUES(9,'legacy-sentinel')")
            connection.commit()
        with patch.dict(os.environ, {'HQ_CHANNEL_DB':legacy_path}):
            with closing(cm.db()):
                pass
            with closing(cm.db()) as connection:
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({
                    'operation_mappings', 'operation_mapping_versions', 'run_snapshots'
                }.issubset(tables))
                self.assertEqual('legacy-sentinel', connection.execute(
                    'SELECT value FROM settings WHERE id=9').fetchone()[0])

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

    def test_operation_mapping_is_versioned_and_managed_route_is_fail_closed(self):
        payload = {'source_page':'banana','provider':'xiaole','prompt':'hello'}
        shadow = cm.save_operation_mapping('admin', {
            'operation_id':'image.xiaole.text', 'state':'shadow',
            'channel':self.ch['id'], 'expected_revision':0,
        })
        self.assertEqual(1, shadow['revision'])
        shadowed = cm.capture('image', payload, invocation_source='agent')
        self.assertNotIn('_channel_binding', shadowed)
        self.assertEqual('image.xiaole.text', shadowed['_channel_shadow']['operation_id'])

        full = cm.reserve(self.ch['id'], 'full')
        cm.finish(full, 'passed', 'artifact checked')
        managed = cm.save_operation_mapping('admin', {
            'operation_id':'image.xiaole.text', 'state':'managed',
            'channel':self.ch['id'], 'expected_revision':1,
        })
        captured = cm.capture('image', payload, invocation_source='agent')
        binding = captured['_channel_binding']
        self.assertEqual(2, managed['revision'])
        self.assertEqual('image.xiaole.text', binding['operation_id'])
        self.assertEqual(2, binding['mapping_revision'])
        self.assertEqual('agent', binding['invocation_source'])
        self.assertEqual('test-model', binding['model'])

        cm.save('admin', dict(self.body, **self.ch, model='new-untested-model'))
        with self.assertRaisesRegex(ValueError, '完整生成测试'):
            cm.capture('image', payload)

        paused = cm.save_operation_mapping('admin', {
            'operation_id':'image.xiaole.text', 'state':'paused',
            'expected_revision':2,
        })
        self.assertEqual(3, paused['revision'])
        with self.assertRaises(ValueError):
            cm.capture('image', payload)
        with self.assertRaisesRegex(ValueError, '刷新'):
            cm.save_operation_mapping('admin', {
                'operation_id':'image.xiaole.text', 'state':'legacy',
                'expected_revision':2,
            })
        restored = cm.rollback_operation_mapping('admin', {
            'operation_id':'image.xiaole.text', 'target_revision':1,
            'expected_revision':3,
        })
        self.assertEqual('shadow', restored['state'])
        self.assertEqual(4, restored['revision'])
        self.assertEqual([4, 3, 2, 1], [
            item['revision'] for item in cm.overview()['operation_mappings'][0]['history']
        ])

    def test_operation_mapping_preserves_ordered_channel_priorities_and_rollback(self):
        second = cm.save('admin', dict(self.body, name='备用图片渠道', secret='second-secret'))
        third = cm.save('admin', dict(self.body, name='候选图片渠道', secret='third-secret'))
        ordered = [self.ch['id'], second['id'], third['id']]
        first = cm.save_operation_mapping('admin', {
            'operation_id':'image.xiaole.text', 'state':'shadow',
            'channels':ordered, 'expected_revision':0,
        })
        self.assertEqual(ordered, first['channels'])
        self.assertEqual(self.ch['id'], first['channel'])
        self.assertEqual(second['id'], first['backup'])

        reordered = [third['id'], self.ch['id'], second['id']]
        second_revision = cm.save_operation_mapping('admin', {
            'operation_id':'image.xiaole.text', 'state':'shadow',
            'channels':reordered, 'expected_revision':1,
        })
        self.assertEqual(reordered, second_revision['channels'])

        restored = cm.rollback_operation_mapping('admin', {
            'operation_id':'image.xiaole.text', 'target_revision':1,
            'expected_revision':2,
        })
        self.assertEqual(ordered, restored['channels'])
        self.assertEqual(3, restored['revision'])

        with self.assertRaisesRegex(ValueError, '重复'):
            cm.save_operation_mapping('admin', {
                'operation_id':'image.xiaole.text', 'state':'shadow',
                'channels':[self.ch['id'], self.ch['id']], 'expected_revision':3,
            })

        from server.content_domains import channel_lifecycle
        disabled = channel_lifecycle.mutate('admin', {
            'id':third['id'], 'version':1, 'action':'disable', 'reason':'准备下线',
        })
        with self.assertRaisesRegex(ValueError, '优先级'):
            channel_lifecycle.mutate('admin', {
                'id':third['id'], 'version':disabled['version'],
                'action':'delete', 'reason':'确认下线',
            })

    def test_managed_capture_seals_ordered_ready_image_candidates(self):
        second = cm.save('admin', dict(
            self.body, name='备用图片渠道', secret='second-secret'))
        for channel in (self.ch, second):
            run_id = cm.reserve(channel['id'], 'full')
            cm.finish(run_id, 'passed', 'artifact checked')
        cm.save_operation_mapping('admin', {
            'operation_id':'image.xiaole.text', 'state':'managed',
            'channels':[self.ch['id'], second['id']], 'expected_revision':0,
        })

        captured = cm.capture('image', {
            'source_page':'banana', 'provider':'xiaole', 'prompt':'hello',
        })
        binding = captured['_channel_binding']
        self.assertEqual([self.ch['id'], second['id']], [
            item['id'] for item in binding['route_candidates']
        ])
        self.assertEqual([self.ch['id'], second['id']], binding['route_order'])
        self.assertEqual(1, binding['route_attempt'])
        self.assertNotIn('secret', json.dumps(binding))

        cm.save('admin', dict(self.body, **second, model='changed-after-capture'))
        with self.assertRaisesRegex(ValueError, '版本已变化'):
            with cm.acceptance_guard([captured]):
                pass

    def test_image_task_fails_over_after_definitive_prebilling_rejection(self):
        second = cm.save('admin', dict(
            self.body, name='备用图片渠道', secret='second-secret'))
        run_id = cm.reserve(second['id'], 'full')   # 切换目标必须具备最近 24 小时完整测试证据
        cm.finish(run_id, 'passed', 'artifact checked')
        binding = {
            'operation_id':'image.xiaole.text', 'mapping_revision':1,
            'id':self.ch['id'], 'version':1, 'adapter':'openai_image',
            'model':'test-model', 'front':'', 'invocation_source':'web',
            'route_order':[self.ch['id'], second['id']], 'route_attempt':1,
            'route_candidates':[
                {'id':self.ch['id'], 'version':1, 'adapter':'openai_image',
                 'model':'test-model'},
                {'id':second['id'], 'version':1, 'adapter':'openai_image',
                 'model':'test-model'},
            ],
        }
        calls = []

        def generate(cfg, payload, _rid, _job_id):
            calls.append(cfg['id'])
            if cfg['id'] == self.ch['id']:
                raise runtime.SubmissionRejected('供应商明确拒绝提交')
            return {'type':'image', 'channel_id':cfg['id']}

        with patch.object(runtime, 'generate', side_effect=generate):
            result = runtime.run_task(binding, {'prompt':'hello'}, 501)

        self.assertEqual([self.ch['id'], second['id']], calls)
        self.assertEqual(second['id'], result['channel_id'])
        evidence = cm.task_evidence(501)
        self.assertEqual('passed', evidence['state'])
        self.assertEqual(second['id'], evidence['channel'])
        self.assertEqual(2, evidence['execution_snapshot']['route_attempt'])
        self.assertEqual(self.ch['id'], evidence['execution_snapshot']['attempts'][0]['channel'])
        self.assertEqual('failed', evidence['execution_snapshot']['attempts'][0]['state'])
        overview_run = next(item for item in cm.overview()['runs'] if item['job_id'] == '501')
        self.assertEqual(self.ch['id'], overview_run['execution_snapshot']['attempts'][0]['channel'])

    def test_image_task_never_fails_over_when_submission_outcome_is_unknown(self):
        second = cm.save('admin', dict(
            self.body, name='备用图片渠道', secret='second-secret'))
        binding = {
            'operation_id':'image.xiaole.text', 'mapping_revision':1,
            'id':self.ch['id'], 'version':1, 'adapter':'openai_image',
            'model':'test-model', 'front':'', 'invocation_source':'web',
            'route_order':[self.ch['id'], second['id']], 'route_attempt':1,
            'route_candidates':[
                {'id':self.ch['id'], 'version':1, 'adapter':'openai_image',
                 'model':'test-model'},
                {'id':second['id'], 'version':1, 'adapter':'openai_image',
                 'model':'test-model'},
            ],
        }
        with patch.object(runtime, 'generate',
                          side_effect=runtime.OutcomeUnknown('提交结果未知')) as generate:
            with self.assertRaises(RuntimeError):
                runtime.run_task(binding, {'prompt':'hello'}, 502)
        self.assertEqual(1, generate.call_count)
        evidence = cm.task_evidence(502)
        self.assertEqual('unknown', evidence['state'])
        self.assertEqual(self.ch['id'], evidence['channel'])

    def test_provider_post_failure_classification_is_conservative(self):
        from server.content_domains import safe_http
        cfg = cm.version(self.ch['id'], 1, True)
        with patch.object(safe_http, 'request_json',
                          side_effect=safe_http.SafeHttpError('unauthorized', 401)):
            with self.assertRaises(runtime.SubmissionRejected):
                runtime.request(cfg, 'POST', '/images/generations', {})
        for status in (0, 408, 409, 425, 429, 500):
            with self.subTest(status=status), patch.object(
                    safe_http, 'request_json',
                    side_effect=safe_http.SafeHttpError('uncertain', status)):
                with self.assertRaises(runtime.OutcomeUnknown):
                    runtime.request(cfg, 'POST', '/images/generations', {})

    def test_queued_failover_resumes_selected_candidate_after_worker_restart(self):
        second = cm.save('admin', dict(
            self.body, name='备用图片渠道', secret='second-secret'))
        ready = cm.reserve(second['id'], 'full')   # 切换目标必须具备最近 24 小时完整测试证据
        cm.finish(ready, 'passed', 'artifact checked')
        candidates = [
            {'id':self.ch['id'], 'version':1, 'adapter':'openai_image',
             'model':'test-model'},
            {'id':second['id'], 'version':1, 'adapter':'openai_image',
             'model':'test-model'},
        ]
        binding = {
            'operation_id':'image.xiaole.text', 'mapping_revision':1,
            **candidates[0], 'front':'', 'invocation_source':'web',
            'route_order':[self.ch['id'], second['id']], 'route_attempt':1,
            'route_candidates':candidates,
        }
        rid = cm.reserve(self.ch['id'], 'task', '503', cm.version(self.ch['id']),
                         execution_snapshot=binding)
        cm.finish(rid, 'running', '执行请求前检查')
        cm.finish_task_failover_safe(rid, '供应商明确拒绝提交')
        cm.prepare_task_failover(rid, candidates[1], '供应商明确拒绝提交')

        seen = []
        def execute(run_id, _payload):
            evidence = cm.task_evidence(503)
            seen.append((run_id, evidence['channel']))
            cm.finish(run_id, 'passed', 'artifact checked')
            return {'channel_id':evidence['channel']}

        with patch.object(runtime, 'execute', side_effect=execute):
            result = runtime.run_task(binding, {'prompt':'hello'}, 503)
        self.assertEqual([(rid, second['id'])], seen)
        self.assertEqual(second['id'], result['channel_id'])

    def test_queued_gate_timeout_switches_channel_before_any_submission(self):
        """并发/限流排队超时属于「未提交」失败：必须切到下一候选，而不是卡在排队。"""
        second = cm.save('admin', dict(
            self.body, name='备用图片渠道', secret='second-secret'))
        run_id = cm.reserve(second['id'], 'full')   # 候补渠道必须有最近 24 小时的完整测试证据
        cm.finish(run_id, 'passed', 'artifact checked')
        candidates = [
            {'id':self.ch['id'], 'version':1, 'adapter':'openai_image',
             'model':'test-model'},
            {'id':second['id'], 'version':1, 'adapter':'openai_image',
             'model':'test-model'},
        ]
        binding = {
            'operation_id':'image.xiaole.text', 'mapping_revision':1,
            **candidates[0], 'front':'', 'invocation_source':'web',
            'route_order':[self.ch['id'], second['id']], 'route_attempt':1,
            'route_candidates':candidates,
        }
        # 占满主渠道（默认并发 2）：新任务只能在排队闸里等，等满 120 秒仍未提交供应商
        busy = [cm.reserve(self.ch['id'], 'task', job, cm.version(self.ch['id']))
                for job in ('504', '506')]
        with closing(cm.db()) as c:
            for run in busy:
                c.execute("UPDATE runs SET state='running' WHERE id=?", (run,))
            c.commit()

        real_monotonic = time.monotonic
        class JumpClock:
            calls = 0
            def __call__(self):
                JumpClock.calls += 1
                return real_monotonic() + (0.0 if JumpClock.calls <= 4 else 500.0)

        def generate(cfg, _payload, _rid, _job_id):
            return {'channel_id':cfg['id']}

        with patch.object(time, 'monotonic', JumpClock()), \
                patch.object(time, 'sleep', lambda *_: None), \
                patch.object(runtime, 'generate', side_effect=generate):
            result = runtime.run_task(binding, {'prompt':'hello'}, 505)

        self.assertEqual(second['id'], result['channel_id'])
        evidence = cm.task_evidence(505)
        self.assertEqual('passed', evidence['state'])
        self.assertEqual(second['id'], evidence['channel'])
        snapshot = evidence['execution_snapshot']
        self.assertEqual(2, snapshot['route_attempt'])
        self.assertEqual(self.ch['id'], snapshot['attempts'][0]['channel'])
        self.assertIn('尚未提交供应商', snapshot['attempts'][0]['detail'])
        self.assertEqual(1, len([run for run in cm.overview()['runs']
                                 if run['job_id'] == '505']))

    def test_failover_skips_candidate_that_lost_readiness(self):
        """采集后被停用的候补渠道不能再接单：切换应跳过它、落到下一个可用候选。"""
        second = cm.save('admin', dict(self.body, name='备用-失效', secret='second-secret'))
        third = cm.save('admin', dict(self.body, name='备用-可用', secret='third-secret'))
        for channel in (self.ch, second, third):
            run_id = cm.reserve(channel['id'], 'full')
            cm.finish(run_id, 'passed', 'artifact checked')
        cm.save_operation_mapping('admin', {
            'operation_id':'image.xiaole.text', 'state':'managed',
            'channels':[self.ch['id'], second['id'], third['id']], 'expected_revision':0,
        })
        binding = cm.capture('image', {
            'source_page':'banana', 'provider':'xiaole', 'prompt':'hello',
        })['_channel_binding']
        cm.save('admin', dict(self.body, **second, enabled=False))

        calls = []
        def generate(cfg, _payload, _rid, _job_id):
            calls.append(cfg['id'])
            if cfg['id'] == self.ch['id']:
                raise runtime.SubmissionRejected('供应商明确拒绝提交')
            return {'channel_id':cfg['id']}

        with patch.object(runtime, 'generate', side_effect=generate):
            result = runtime.run_task(binding, {'prompt':'hello'}, 601)

        self.assertEqual([self.ch['id'], third['id']], calls)
        self.assertEqual(third['id'], result['channel_id'])
        evidence = cm.task_evidence(601)
        self.assertEqual(third['id'], evidence['channel'])
        self.assertEqual(2, evidence['execution_snapshot']['route_attempt'])

    def test_stranded_queued_run_converges_instead_of_queue_loop(self):
        """排队记录指向候选清单之外的渠道时必须收敛成终态，否则会在排队与重排之间空转。"""
        backup = cm.save('admin', dict(self.body, name='备用-固化', secret='second-secret'))
        candidates = [
            {'id':self.ch['id'], 'version':1, 'adapter':'openai_image', 'model':'test-model'},
            {'id':backup['id'], 'version':1, 'adapter':'openai_image', 'model':'test-model'},
        ]
        switched = {
            'operation_id':'image.xiaole.text', 'mapping_revision':1, **candidates[1],
            'front':'', 'invocation_source':'web',
            'route_order':[self.ch['id'], backup['id']], 'route_attempt':2,
            'route_candidates':candidates,
        }
        cm.reserve(backup['id'], 'task', '602', cm.version(backup['id']),
                   execution_snapshot=switched)
        incoming = {
            'operation_id':'image.xiaole.text', 'mapping_revision':9, **candidates[0],
            'route_candidates':[candidates[0]],
        }

        with patch.object(runtime, 'execute') as execute:
            with self.assertRaises(RuntimeError) as raised:
                runtime.run_task(incoming, {'prompt':'hello'}, 602)

        self.assertIn('不在候选清单中', str(raised.exception))
        self.assertFalse(execute.called)
        evidence = cm.task_evidence(602)
        self.assertEqual('failed', evidence['state'])
        self.assertEqual(backup['id'], evidence['channel'])

    def test_refused_failover_bookkeeping_converges_to_terminal_state(self):
        """记账被拒时不能把裸 ValueError 抛出循环：必须落成终态，让调用方按失败退点。"""
        backup = cm.save('admin', dict(self.body, name='备用-记账', secret='second-secret'))
        candidates = [
            {'id':self.ch['id'], 'version':1, 'adapter':'openai_image', 'model':'test-model'},
            {'id':backup['id'], 'version':1, 'adapter':'openai_image', 'model':'test-model'},
        ]
        binding = {
            'operation_id':'image.xiaole.text', 'mapping_revision':1, **candidates[0],
            'front':'', 'invocation_source':'web',
            'route_order':[self.ch['id'], backup['id']], 'route_attempt':1,
            'route_candidates':candidates,
        }

        def generate(cfg, _payload, _rid, _job_id):
            if cfg['id'] == self.ch['id']:
                raise runtime.SubmissionRejected('供应商明确拒绝提交')
            return {'channel_id':cfg['id']}

        with patch.object(runtime, 'generate', side_effect=generate), \
                patch.object(cm, 'finish_task_failover_safe',
                             side_effect=ValueError('当前任务状态不允许自动切换渠道')):
            with self.assertRaises(RuntimeError) as raised:
                runtime.run_task(binding, {'prompt':'hello'}, 603)

        self.assertIn('供应商明确拒绝提交', str(raised.exception))
        evidence = cm.task_evidence(603)
        self.assertEqual('failed', evidence['state'])
        self.assertEqual(self.ch['id'], evidence['channel'])

    def test_operation_publish_serializes_concurrent_channel_change(self):
        full = cm.reserve(self.ch['id'], 'full')
        cm.finish(full, 'passed', 'artifact checked')
        validated = threading.Event()
        release = threading.Event()
        original = cm._mapping_channel

        def pause_after_validation(*args, **kwargs):
            result = original(*args, **kwargs)
            validated.set()
            release.wait(2)
            return result

        with patch.object(cm, '_mapping_channel', side_effect=pause_after_validation), \
                ThreadPoolExecutor(max_workers=2) as pool:
            publish = pool.submit(cm.save_operation_mapping, 'admin', {
                'operation_id':'image.xiaole.text', 'state':'managed',
                'channel':self.ch['id'], 'expected_revision':0,
            })
            self.assertTrue(validated.wait(1))
            update = pool.submit(
                cm.save, 'admin', dict(self.body, **self.ch, model='new-model'))
            try:
                time.sleep(.1)
                self.assertFalse(update.done(), 'channel update crossed the publish transaction')
            finally:
                release.set()
            self.assertEqual('managed', publish.result(timeout=2)['state'])
            self.assertEqual(2, update.result(timeout=2)['version'])

    def test_managed_acceptance_rechecks_after_charge_and_refunds_stale_route(self):
        from server.content_domains import jobs_store
        full = cm.reserve(self.ch['id'], 'full')
        cm.finish(full, 'passed', 'artifact checked')
        cm.save_operation_mapping('admin', {
            'operation_id':'image.xiaole.text', 'state':'managed',
            'channel':self.ch['id'], 'expected_revision':0,
        })
        refunds = []

        def change_channel(_connection, _job_id):
            cm.save('admin', dict(self.body, **self.ch, model='untested-model'))

        with self.assertRaises(jobs_store.PaidJobInsertError):
            jobs_store.create_paid_job(
                self.job_db, lambda *_args: 90,
                lambda username, cost, *_args, **_kwargs: refunds.append((username, cost)) or True,
                'image', 'u', 1,
                {'source_page':'banana','provider':'xiaole','prompt':'hello'},
                'content', before_commit=change_channel, invocation_source='web')
        self.assertEqual([('u', 1)], refunds)
        with closing(self.job_db()) as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0])

    def test_managed_acceptance_holds_channel_lock_through_job_commit(self):
        from server.content_domains import jobs_store
        full = cm.reserve(self.ch['id'], 'full')
        cm.finish(full, 'passed', 'artifact checked')
        cm.save_operation_mapping('admin', {
            'operation_id':'image.xiaole.text', 'state':'managed',
            'channel':self.ch['id'], 'expected_revision':0,
        })
        with closing(self.job_db()):
            pass
        committing = threading.Event()
        release = threading.Event()
        path = self.tmp.name + '/jobs.db'

        class BlockingConnection(sqlite3.Connection):
            def commit(connection):
                committing.set()
                release.wait(2)
                return super().commit()

        def blocking_job_db():
            connection = sqlite3.connect(
                path, timeout=10, factory=BlockingConnection)
            connection.row_factory = sqlite3.Row
            return connection

        with ThreadPoolExecutor(max_workers=2) as pool:
            submission = pool.submit(
                jobs_store.create_paid_job,
                blocking_job_db, lambda *_args: 90,
                lambda *_args, **_kwargs: True, 'image', 'u', 1,
                {'source_page':'banana','provider':'xiaole','prompt':'hello'},
                'content', None, '', None, '', 'web')
            self.assertTrue(committing.wait(1))
            update = pool.submit(
                cm.save, 'admin', dict(self.body, **self.ch, model='new-model'))
            try:
                time.sleep(.1)
                self.assertFalse(update.done(), 'channel changed before job commit completed')
            finally:
                release.set()
            self.assertGreater(submission.result(timeout=2)[0], 0)
            self.assertEqual(2, update.result(timeout=2)['version'])

    def test_shadow_job_persists_observable_server_snapshot(self):
        from server.content_domains import jobs_store
        cm.save_operation_mapping('admin', {
            'operation_id':'image.xiaole.text', 'state':'shadow',
            'channel':self.ch['id'], 'expected_revision':0,
        })
        job_id, _ = jobs_store.create_paid_job(
            self.job_db, lambda *_args: 90, lambda *_args, **_kwargs: True,
            'image', 'u', 1,
            {'source_page':'banana','provider':'xiaole','prompt':'hello',
             '_channel_shadow':{'operation_id':'forged','id':'attacker'}},
            'content', invocation_source='agent')
        evidence = cm.task_evidence(job_id)
        self.assertEqual('captured', evidence['state'])
        self.assertEqual('image.xiaole.text', evidence['operation_id'])
        self.assertEqual('agent', evidence['invocation_source'])
        self.assertEqual(self.ch['id'], evidence['execution_snapshot']['id'])
        self.assertEqual({str(job_id)}, cm.search_task_ids('image.xiaole.text'))
        with closing(self.job_db()) as connection:
            payload = json.loads(connection.execute(
                'SELECT payload FROM jobs WHERE id=?', (job_id,)).fetchone()[0])
        self.assertIn('_channel_shadow', payload)
        self.assertNotIn('_channel_binding', payload)

    def test_shadow_projection_recovers_after_post_commit_crash_gap(self):
        from server.content_domains import jobs_store
        cm.save_operation_mapping('admin', {
            'operation_id':'image.xiaole.text', 'state':'shadow',
            'channel':self.ch['id'], 'expected_revision':0,
        })
        with patch.object(cm, 'record_shadow', side_effect=RuntimeError('simulated crash gap')):
            job_id, _ = jobs_store.create_paid_job(
                self.job_db, lambda *_args: 90, lambda *_args, **_kwargs: True,
                'image', 'u', 1,
                {'source_page':'banana','provider':'xiaole','prompt':'hello'},
                'content', invocation_source='web')
        self.assertEqual({}, cm.task_evidence(job_id))
        self.assertEqual(1, jobs_store.reconcile_shadow_observations(self.job_db))
        evidence = cm.task_evidence(job_id)
        self.assertEqual('image.xiaole.text', evidence['operation_id'])
        observation_id = evidence['execution_snapshot']['observation_id']
        with closing(self.job_db()) as connection:
            stored_payload = json.loads(connection.execute(
                'SELECT payload FROM jobs WHERE id=?', (job_id,)).fetchone()[0])
        self.assertEqual(observation_id, cm.record_shadow(
            job_id, stored_payload['_channel_shadow']))
        with closing(cm.db()) as connection:
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM runs WHERE id=? AND kind='shadow'",
                (observation_id,)).fetchone()[0])

    def test_shadow_identity_does_not_alias_reused_job_id(self):
        common = {
            'operation_id':'image.xiaole.text', 'mapping_revision':1,
            'id':self.ch['id'], 'version':1, 'adapter':'openai_image',
            'model':'test-model', 'invocation_source':'web',
        }
        first = cm._seal_shadow(dict(common, observation_id='a' * 32))
        second = cm._seal_shadow(dict(
            common, observation_id='b' * 32, mapping_revision=2))
        self.assertEqual('a' * 32, cm.record_shadow(7, first))
        self.assertEqual('b' * 32, cm.record_shadow(7, second))
        with closing(cm.db()) as connection:
            self.assertEqual(2, connection.execute(
                "SELECT COUNT(*) FROM runs WHERE job_id='7' AND kind='shadow'").fetchone()[0])
        self.assertEqual('b' * 32,
                         cm.task_evidence(7)['execution_snapshot']['observation_id'])

    def test_shadow_projection_rejects_unsealed_job_payload(self):
        from server.content_domains import jobs_store
        with closing(self.job_db()) as connection:
            cursor = connection.execute(
                "INSERT INTO jobs(kind,username,cost,payload,created_at,updated_at,owner) "
                "VALUES('image','u',0,?,1,1,'content')",
                (json.dumps({'_channel_shadow': {
                    'operation_id':'image.xiaole.text',
                    'observation_id':'c' * 32,
                    'id':self.ch['id'],
                }}),))
            job_id = cursor.lastrowid
            connection.commit()
        self.assertEqual(0, jobs_store.reconcile_shadow_observations(self.job_db))
        self.assertEqual({}, cm.task_evidence(job_id))

    def test_invocation_source_requires_matching_internal_token(self):
        from server.content_domains import core
        handler = types.SimpleNamespace(headers={})
        with patch.object(core, 'AUTH_INTERNAL_TOKEN', 'trusted-token'):
            self.assertEqual('web', core._invocation_source(handler))
            handler.headers = {'X-HQ-Internal-Token':'wrong-token'}
            self.assertEqual('web', core._invocation_source(handler))
            handler.headers = {'X-HQ-Internal-Token':'trusted-token'}
            self.assertEqual('agent', core._invocation_source(handler))

    def test_managed_publish_requires_fresh_full_generation_evidence(self):
        with self.assertRaisesRegex(ValueError, '完整生成测试'):
            cm.save_operation_mapping('admin', {
                'operation_id':'image.xiaole.text', 'state':'managed',
                'channel':self.ch['id'], 'expected_revision':0,
            })

    def test_operation_mapping_rejects_missing_reference_capability(self):
        with self.assertRaisesRegex(ValueError, '参考图'):
            cm.save_operation_mapping('admin', {
                'operation_id':'image.xiaole.reference', 'state':'shadow',
                'channel':self.ch['id'], 'expected_revision':0,
            })

    def test_recycle_bin_blocks_operation_mapping_references(self):
        from server.content_domains import channel_lifecycle
        cm.save_operation_mapping('admin', {
            'operation_id':'image.xiaole.text', 'state':'shadow',
            'channel':self.ch['id'], 'expected_revision':0,
        })
        disabled = channel_lifecycle.mutate('admin', {
            'id':self.ch['id'], 'version':1, 'action':'disable', 'reason':'maintenance',
        })
        with self.assertRaisesRegex(ValueError, '映射引用'):
            channel_lifecycle.mutate('admin', {
                'id':self.ch['id'], 'version':disabled['version'],
                'action':'delete', 'reason':'retire',
            })

    def test_task_evidence_contains_immutable_operation_snapshot(self):
        snapshot = {
            'operation_id':'image.xiaole.text', 'mapping_revision':4,
            'id':self.ch['id'], 'version':1, 'adapter':'openai_image',
            'model':'test-model', 'invocation_source':'agent',
        }
        rid = cm.reserve(self.ch['id'], 'task', 'snapshot-92',
                         execution_snapshot=snapshot)
        cm.finish(rid, 'running', 'accepted', 'provider-92')
        evidence = cm.task_evidence('snapshot-92')
        self.assertEqual('image.xiaole.text', evidence['operation_id'])
        self.assertEqual(4, evidence['mapping_revision'])
        self.assertEqual('agent', evidence['invocation_source'])
        self.assertEqual('test-model', evidence['execution_snapshot']['model'])
        self.assertEqual({'snapshot-92'}, cm.search_task_ids('image.xiaole.text'))

    def test_overview_exposes_shared_operation_catalog(self):
        item = next(x for x in cm.overview()['operations']
                    if x['operation_id'] == 'image.xiaole.text')
        self.assertEqual('image', item['channel_kind'])
        self.assertEqual(['image-generate'], item['agent_capabilities'])
        self.assertIsNone(item['mapping'])

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
