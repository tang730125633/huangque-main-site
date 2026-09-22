# -*- coding: utf-8 -*-
"""旧任务场景：先创建待执行的 A 任务 → 切到 B → 放行执行，旧任务仍走 A。

验收要点（用户指定）：
  * 绑定发生在【受理时】，不是在执行时；
  * 所以受理期间切换渠道，不会把已经受理的旧任务改到新渠道；
  * 新任务走 B；
  * 原渠道仍可见、可切回。

控制执行时机的方式：隔离设施用 HQ_WORKER_PAUSED=1 不启动 worker 池，
受理后任务停在队列里；切完渠道再 POST /__start-workers 放行。
这是**隔离测试设施**的开关，生产没有这个能力，也不需要为测试新增。
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path(os.environ.get('HQ_REPO') or Path(__file__).resolve().parents[2])
FUNC = 'image.banana.nb2.text'


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class PausedWorkerE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        cls.data_dir = tempfile.mkdtemp(prefix='hq-old-')
        env = dict(os.environ)
        env.update({'HQ_REPO': str(REPO), 'HQ_SWITCH_PORT': str(cls.port),
                    'HQ_MOCK_PROVIDER_PORT': str(_free_port()),
                    'HQ_SWITCH_DATA': cls.data_dir,
                    'HQ_WORKER_PAUSED': '1'})
        cls.proc = subprocess.Popen([sys.executable, str(HERE / 'local_service.py')],
                                    cwd=str(HERE), env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        cls.base = 'http://127.0.0.1:%d' % cls.port
        for _ in range(60):
            try:
                urllib.request.urlopen(cls.base + '/api/auth/me', timeout=2).read()
                return
            except Exception:
                if cls.proc.poll() is not None:
                    raise AssertionError('隔离服务启动失败：'
                                         + cls.proc.stdout.read().decode('utf-8', 'replace')[-800:])
                time.sleep(0.5)
        raise AssertionError('隔离服务未就绪')

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()

    def req(self, path, body=None, token=False):
        data = json.dumps(body).encode() if body is not None else None
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer local-token'
        r = urllib.request.Request(self.base + path, data=data, headers=headers,
                                   method='POST' if body is not None else 'GET')
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b'{}')
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b'{}')

    def channels(self):
        return self.req('/api/admin/channel-manager')[1]

    def switch(self, cid, attempts=3):
        """乐观锁：版本号每次都读当前值；并发/时序导致冲突时重读重试。"""
        last = (0, {})
        for _ in range(attempts):
            rev = 0
            for m in self.channels().get('operation_mappings') or []:
                if m['operation_id'] == FUNC:
                    rev = m['revision']
            status, body = self.req('/api/admin/channel-manager/operation-mapping',
                                    {'operation_id': FUNC, 'state': 'managed',
                                     'channels': [cid], 'expected_revision': rev})
            if status == 200:
                return status, body
            last = (status, body)
            time.sleep(0.3)
        return last

    def catalog_entry(self):
        for item in self.req('/api/gen/channel-parameters')[1].get('items') or []:
            if item.get('operation_id') == FUNC:
                return item
        return None

    def submit(self):
        entry = self.catalog_entry()
        self.assertIsNotNone(entry, '目录里必须有这个功能的条目')
        return entry, self.req('/api/gen/banana',
                               {'prompt': '一只猫', 'model': 'nb2', 'quality': 'std',
                                'count': 1, 'source_page': 'banana',
                                'parameter_selection': {
                                    'revision': entry['revision'],
                                    'combination': entry['combinations'][0]['id']}},
                               token=True)

    def job_row(self, job_id):
        import sqlite3
        con = sqlite3.connect(str(Path(self.data_dir) / 'content_jobs.db'))
        con.row_factory = sqlite3.Row
        row = con.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        con.close()
        return dict(row) if row else {}

    def wait_job(self, job_id, seconds=40):
        deadline = time.time() + seconds
        row = {}
        while time.time() < deadline:
            row = self.job_row(job_id)
            if str(row.get('status')) in ('done', 'error', 'failed'):
                return row
            time.sleep(0.3)
        return row

    def posts(self):
        _, d = self.req('/__provider-calls')
        return [c for c in (d.get('calls') or []) if c.get('method') == 'POST']

    def test_old_task_keeps_its_channel_after_switch(self):
        ov = self.channels()
        a = [i for i in ov['items'] if i['model'] == 'gpt-image-2'][0]
        b = [i for i in ov['items'] if i['model'] == 'seedream-5.0-pro'][0]

        # ① 指向 A，受理一个任务；此时 worker 暂停，任务只入库不执行
        st, body = self.switch(a['id'])
        self.assertEqual(st, 200, body)
        entry_a, (st, job_a) = self.submit()
        self.assertEqual(st, 200, job_a)
        row_a = self.job_row(job_a['job_id'])
        self.assertEqual(row_a.get('status'), 'pending', 'worker 暂停时任务应停在待执行')
        self.assertEqual(self.posts(), [], '受理阶段不该调用供应商')

        # ② 受理完成后切到 B
        st, body = self.switch(b['id'])
        self.assertEqual(st, 200, body)
        self.assertEqual(self.catalog_entry()['front'], entry_a['front'],
                         '功能身份不变，只是有效渠道换了')

        # ③ 放行 worker：旧任务执行时仍必须走受理时绑定的 A
        st, _ = self.req('/__start-workers', {})
        self.assertEqual(st, 200)
        row_a = self.wait_job(job_a['job_id'])
        self.assertEqual(row_a.get('status'), 'done', row_a.get('error'))
        call_a = self.posts()[-1]
        self.assertTrue(call_a['path'].startswith('/openai-image'),
                        '旧任务应走 A 的地址，实际 %s' % call_a['path'])
        self.assertEqual(call_a['authorization'], 'Bearer secret-of-A-aaaa')
        self.assertEqual(call_a['body']['model'], 'gpt-image-2')

        # ④ 新任务走 B
        _, (st, job_b) = self.submit()
        self.assertEqual(st, 200, job_b)
        row_b = self.wait_job(job_b['job_id'])
        self.assertEqual(row_b.get('status'), 'done', row_b.get('error'))
        call_b = self.posts()[-1]
        self.assertTrue(call_b['path'].startswith('/lechuang-image'), call_b['path'])
        self.assertEqual(call_b['authorization'], 'Bearer secret-of-B-bbbb')
        self.assertEqual(call_b['body']['model'], 'seedream-5.0-pro')

        # ⑤ 原渠道仍可见、可切回
        ids = [i['id'] for i in self.channels()['items']]
        self.assertIn(a['id'], ids)
        st, body = self.switch(a['id'])
        self.assertEqual(st, 200, body)


if __name__ == '__main__':
    unittest.main(verbosity=2)
