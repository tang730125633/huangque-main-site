import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import tests.test_channel_manager as base
from server.content_domains import channel_manager as cm, channel_runtime as runtime
from server.content_domains import safe_http, task_termination as termination


def _local_resolver(host, port, type=0):
    # 本地沙箱 DNS 会把未知域名解析成保留网段；测试中固定返回公网 IP。
    if host in ('127.0.0.1', 'localhost'):
        return [(2, 1, 6, '', ('127.0.0.1', port))]
    return [(2, 1, 6, '', ('93.184.216.34', port))]


class ManagedChannelTerminationTests(unittest.TestCase):
    def setUp(self):
        base.ChannelTests.setUp(self)
        real_validate = safe_http.validate_target
        self._resolver_patch = mock.patch.object(
            safe_http, 'validate_target',
            side_effect=lambda url, proxy=False: real_validate(url, proxy=proxy, resolver=_local_resolver))
        self._resolver_patch.start()
        self.addCleanup(self._resolver_patch.stop)

    def make_jobs_db(self, kind='image', payload=None, cost=12, status='running'):
        handle = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        handle.close()
        path = Path(handle.name)
        self.addCleanup(lambda: path.unlink(missing_ok=True))

        def factory():
            conn = sqlite3.connect(path, timeout=10)
            conn.row_factory = sqlite3.Row
            return conn

        with closing(factory()) as c:
            c.execute('''CREATE TABLE jobs(id INTEGER PRIMARY KEY, kind TEXT, username TEXT,
                cost INTEGER, status TEXT, payload TEXT, result TEXT, error TEXT,
                updated_at INTEGER, refunded INTEGER DEFAULT 0)''')
            c.execute('INSERT INTO jobs VALUES(1,?,?,?,?,?,NULL,NULL,0,0)',
                      (kind, 'customer', cost, status, json.dumps(payload or {})))
            c.commit()
        return factory

    def managed_job(self, kind='image', status='running'):
        payload = {'_channel_binding': {'id': self.ch['id'], 'version': 1, 'front': 'x'}}
        return self.make_jobs_db(kind=kind, payload=payload, status=status)

    def lechuang_channel(self, adapter='lechuang_image', model='gpt-image-2'):
        body = dict(self.body, name='乐创', adapter=adapter, model=model,
                    base_url='https://api.lechuang.chat/api/v1')
        return cm.save('admin', body)

    def test_managed_image_and_video_tasks_are_terminable(self):
        for kind in ('image', 'xiaole_video', 'sora_video'):
            with self.subTest(kind=kind):
                jobs = self.managed_job(kind=kind)
                with closing(jobs()) as c:
                    described = termination.describe(c, 1)
                self.assertTrue(described['can_terminate'], '托管渠道任务应可终止')
                self.assertEqual(described['unavailable_reason'], '')
                result = termination.request(jobs, 1, 'admin', '客户要求停止')
                self.assertEqual(result['termination']['remote_state'], 'unconfirmed')
                self.assertEqual(result['termination']['refund_state'], 'pending')
                with closing(jobs()) as c:
                    row = c.execute('SELECT status,refunded,error FROM jobs WHERE id=1').fetchone()
                self.assertEqual(row['status'], 'error')
                self.assertEqual(row['refunded'], 2, '有费用的任务要留待退款标记')
                self.assertIn('管理员终止', row['error'])

    def test_non_managed_kind_still_unsupported(self):
        jobs = self.make_jobs_db(kind='image', payload={})   # 老线路生图：没有 _channel_binding
        with closing(jobs()) as c:
            described = termination.describe(c, 1)
        self.assertFalse(described['can_terminate'])
        self.assertIn('尚未接入终止能力', described['unavailable_reason'])
        with self.assertRaises(ValueError):
            termination.request(jobs, 1, 'admin', '客户要求停止')

    def test_terminating_before_start_marks_run_terminated_without_provider_call(self):
        jobs = self.managed_job()
        channel = self.lechuang_channel()
        termination.request(jobs, 1, 'admin', '排队期间取消')
        calls = []
        with mock.patch.object(runtime, 'request', side_effect=lambda *a, **k: calls.append(a)):
            with self.assertRaises(termination.TaskTerminated):
                runtime.run_task({'id': channel['id'], 'version': 1}, {'prompt': 'x', 'count': 1}, 1, jobs)
        self.assertEqual(calls, [], '排队期间终止不得提交供应商')
        with closing(cm.db()) as c:
            states = [r['state'] for r in c.execute(
                "SELECT state FROM runs WHERE channel=? AND kind='task'", (channel['id'],))]
        self.assertEqual(states, ['terminated'])

    def test_terminating_while_polling_keeps_provider_order_and_marks_terminated(self):
        jobs = self.managed_job()
        channel = self.lechuang_channel()

        def fake_request(cfg, method, path, body=None, extra_headers=None, files=None):
            if method == 'POST':
                termination.request(jobs, 1, 'admin', '等待中取消')
                return {'code': 200, 'message': 'success', 'data': {
                    'request_id': 'REQ-TERM-1', 'status': 'pending',
                    'output': {'text': None, 'images': [], 'videos': []}, 'error': None}}
            raise AssertionError('终止后不应继续轮询供应商')

        with mock.patch.object(runtime, 'request', side_effect=fake_request):
            with self.assertRaises(termination.TaskTerminated):
                runtime.run_task({'id': channel['id'], 'version': 1},
                                 {'prompt': 'x', 'count': 1, 'size': '1024x1024',
                                  'quality': 'low', 'background': 'opaque'}, 1, jobs)
        with closing(cm.db()) as c:
            run = c.execute("SELECT state,provider_id FROM runs WHERE channel=? AND kind='task'",
                            (channel['id'],)).fetchone()
            incidents = c.execute('SELECT COUNT(*) FROM channel_incidents WHERE channel=?',
                                  (channel['id'],)).fetchone()[0]
        self.assertEqual(run['state'], 'terminated')
        self.assertEqual(run['provider_id'], 'REQ-TERM-1', '上游工单号必须保留供对账')
        self.assertEqual(incidents, 0, '被终止的运行不得记成渠道故障')
        with closing(jobs()) as c:
            info = termination.describe(c, 1)['termination']
        self.assertEqual(info['provider_id'], 'REQ-TERM-1')
        self.assertEqual(info['remote_state'], 'unconfirmed')

    def test_run_without_job_db_keeps_previous_behaviour(self):
        channel = self.lechuang_channel()
        seen = []

        def fake_request(cfg, method, path, body=None, extra_headers=None, files=None):
            seen.append((method, path))
            return {'code': 200, 'message': 'success', 'data': {
                'request_id': '', 'status': 'pending',
                'output': {'text': None, 'images': [], 'videos': []},
                'error': {'message': '测试用拒绝'}}}

        with mock.patch.object(runtime, 'request', side_effect=fake_request):
            with self.assertRaises(RuntimeError) as ctx:
                runtime.run_task({'id': channel['id'], 'version': 1}, {'prompt': 'x', 'count': 1}, 99)
        self.assertNotIsInstance(ctx.exception, termination.TaskTerminated)
        self.assertEqual(seen, [('POST', '/generations')], '没有终止作用域时按原流程正常提交')


if __name__ == '__main__':
    unittest.main()
