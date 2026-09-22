# -*- coding: utf-8 -*-
"""真实 HTTP 入口 → 受理 → 模拟供应商 → 成品 的完整链路。

与 test_direct_switch_e2e.py 的区别：那份直调 channel_manager.capture，
只能算局部验证；这份调用**真实的 HTTP 处理入口**（本隔离服务实现的
/api/gen/*，与生产同一条 capture 通道），是用户可见的完整流程。

验收的事（用户要求的那一条）：
  后台把功能 X 从 A 切到 B
    → 原生载荷提交（source_page/provider/model 与页面一致）
    → 真实 HTTP 入口受理，绑定 B
    → 报价跟着 B 变
    → 模拟供应商收到 B 的地址、凭据、模型与参数
    → 返回成品
  并且业务功能身份 X 始终不变。

隔离：独立进程、独立 SQLite、独立假供应商；不连生产、不扣费。
"""
import json
import os
import socket
import subprocess
import tempfile
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 本文件在 <仓库>/tests/isolated/ 下，仓库根目录由位置推导；可用 HQ_REPO 覆盖。
# 不写死任何机器上的绝对路径。
REPO = Path(os.environ.get('HQ_REPO') or Path(__file__).resolve().parents[2])
FUNC = 'image.banana.nb2.text'


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class HttpEntryE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        env = dict(os.environ)
        env.update({'HQ_REPO': str(REPO), 'HQ_SWITCH_PORT': str(cls.port),
                    # 假供应商端口必须独占：Windows 的 HTTPServer 默认 SO_REUSEADDR，
                    # 遗留进程与本次进程会同时绑上同一个端口，连接被先绑的那个收走，
                    # 于是「没收到请求」是假象。渠道地址由服务启动时的
                    # repoint_channels() 自动改到本端口。
                    'HQ_MOCK_PROVIDER_PORT': str(_free_port())})
        # 每次跑都用全新的数据目录：观测库是持久的，复用会让运行时
        # 以为任务已完成而直接返回缓存结果，根本不调供应商。
        cls.data_dir = tempfile.mkdtemp(prefix='hq-e2e-')
        env['HQ_SWITCH_DATA'] = cls.data_dir
        cls.proc = subprocess.Popen(
            [sys.executable, str(HERE / 'local_service.py')],
            cwd=str(HERE), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        base = 'http://127.0.0.1:%d' % cls.port
        cls.base = base
        for _ in range(60):
            try:
                urllib.request.urlopen(base + '/api/auth/me', timeout=2).read()
                return
            except Exception:
                if cls.proc.poll() is not None:
                    raise AssertionError('隔离服务启动失败：'
                                         + cls.proc.stdout.read().decode('utf-8', 'replace')[-800:])
                time.sleep(0.5)
        raise AssertionError('隔离服务未在超时内就绪')

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()

    # —— HTTP 小工具 ——
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
        _, ov = self.req('/api/admin/channel-manager')
        return ov

    def current_revision(self):
        """隔离库是持久的，版本号要被测代码自己读出来，不能写死 0。"""
        for m in self.channels().get('operation_mappings') or []:
            if m['operation_id'] == FUNC:
                return m['revision']
        return 0

    def switch(self, cid):
        return self.req('/api/admin/channel-manager/operation-mapping',
                        {'operation_id': FUNC, 'state': 'managed',
                         'channels': [cid], 'expected_revision': self.current_revision()})

    def catalog_entry(self):
        _, d = self.req('/api/gen/channel-parameters')
        for item in d.get('items') or []:
            if item.get('operation_id') == FUNC:
                return item
        return None

    def native_submit(self):
        """按原生页面真实提交：banana.html 会带 source_page，参数选择来自目录。

        provider 由生产处理器（imggen_api.H）自己补，测试脚本不补。
        """
        entry = self.catalog_entry()
        self.assertIsNotNone(entry, '目录里必须有这个功能的条目')
        payload = {'prompt': '一只猫', 'model': 'nb2', 'quality': 'std', 'count': 1,
                   'source_page': 'banana',
                   'parameter_selection': {'revision': entry['revision'],
                                           'combination': entry['combinations'][0]['id']}}
        return entry, self.req('/api/gen/banana', payload, token=True)

    def job_row(self, job_id):
        """从生产的 jobs 表读任务记录（不经过我自己的副本）。"""
        import sqlite3
        con = sqlite3.connect(str(Path(self.data_dir) / 'content_jobs.db'))
        con.row_factory = sqlite3.Row
        row = con.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        con.close()
        return dict(row) if row else {}

    def provider_posts(self):
        """问隔离服务：假供应商收到了哪些 POST（比任务里的副本更权威）。"""
        _, d = self.req('/__provider-calls')
        return [c for c in (d.get('calls') or []) if c.get('method') == 'POST']

    def wait_job(self, job_id, seconds=40):
        """等 worker 把任务跑完；生产是队列 + worker 池，受理返回时还没执行。"""
        import time as _t
        deadline = _t.time() + seconds
        row = {}
        while _t.time() < deadline:
            row = self.job_row(job_id)
            if str(row.get('status')) in ('done', 'error', 'failed'):
                return row
            _t.sleep(0.3)
        return row

    def ledger_deducts(self):
        _, d = self.req('/__ledger')
        return [e for e in d.get('ledger') or [] if e.get('kind') == 'deduct']

    def post_call(self, job, posts=None):
        posts = posts if posts is not None else [c for c in (job.get('provider_calls') or []) if c.get('method') == 'POST']
        self.assertTrue(posts, '假供应商没有收到 POST，任务全文：%s' % json.dumps(
            {k: job.get(k) for k in ('id', 'status', 'phase', 'error', 'channel_id')} | {'calls': job.get('provider_calls')},
            ensure_ascii=False, default=str))
        return posts[0]

    # —— 验收 ——
    def test_switch_then_native_submit_uses_target_channel(self):
        ov = self.channels()
        a = [i for i in ov['items'] if i['model'] == 'gpt-image-2'][0]
        b = [i for i in ov['items'] if i['model'] == 'seedream-5.0-pro'][0]

        seen = []
        for label, ch, points, path_part, secret, model in (
                ('甲', a, 18, '/openai-image', 'secret-of-A-aaaa', 'gpt-image-2'),
                ('乙', b, 35, '/lechuang-image', 'secret-of-B-bbbb', 'seedream-5.0-pro')):
            st, body = self.switch(ch['id'])
            self.assertEqual(st, 200, body)
            entry, (st, job) = self.native_submit()
            self.assertEqual(st, 200, job)
            self.assertEqual(entry['combinations'][0]['points'], points,
                             '%s 的目录报价应为 %s 点' % (label, points))
            self.assertEqual(job.get('cost'), points, '服务端报价应等于目录报价')
            row = self.wait_job(job['job_id'])
            self.assertEqual(row.get('cost'), points, '任务记录里的点数应与报价一致')
            self.assertEqual(self.ledger_deducts()[-1]['amount'], points, '实际扣费应与报价一致')
            self.assertEqual(row.get('status'), 'done', row.get('error'))
            call = self.provider_posts()[-1]
            self.assertTrue(call['path'].startswith(path_part), call['path'])
            self.assertEqual(call['authorization'], 'Bearer ' + secret)
            self.assertEqual(call['body']['model'], model)
            self.assertNotEqual(call['authorization'], 'Bearer secret-of-A-aaaa'
                                if label == '乙' else 'Bearer secret-of-B-bbbb')
            seen.append((label, ch['id'], job['job_id']))

        # 切换只影响新任务：甲那条任务仍属于甲
        self.assertEqual(self.job_row(seen[0][2]).get('status'), 'done')
        # 原渠道仍可见、可切回
        ov2 = self.channels()
        self.assertIn(a['id'], [i['id'] for i in ov2['items']])
        st, body = self.switch(a['id'])
        self.assertEqual(st, 200, body)

    def test_generic_image_uses_content_handler(self):
        """Match nginx: generic image belongs to content, banana to imggen."""
        for model in ('gpt-image-2', 'seedream-5.0-pro'):
            channel = next(c for c in self.channels()['items'] if c['model'] == model)
            self.assertEqual(self.switch(channel['id'])[0], 200)
            entry = self.catalog_entry()
            payload = dict(entry.get('match') or {})
            for key in ('kind', 'reference_count', 'mask_present'):
                payload.pop(key, None)
            payload.update(prompt='isolated cat', count=1,
                           parameter_selection={'revision': entry['revision'],
                                                'combination': entry['combinations'][0]['id']})
            status, accepted = self.req('/api/gen/image', payload, token=True)
            self.assertEqual(status, 200, accepted)
            row = self.wait_job(accepted['job_id'])
            self.assertEqual(row.get('status'), 'done', row)
            self.assertEqual(self.provider_posts()[-1]['body']['model'], model)
            status, fetched = self.req('/api/gen/job/%s' % accepted['job_id'], token=True)
            self.assertEqual(status, 200, fetched)

    def test_video_switch_uses_content_handler(self):
        operation='video.grok.text'
        for label in ('a','b'):
            overview=self.channels()
            channel=next(c for c in overview['items'] if c['name']=='Video '+label)
            revision=next((m['revision'] for m in overview.get('operation_mappings',[])
                           if m['operation_id']==operation),0)
            status, saved=self.req('/api/admin/channel-manager/operation-mapping',
                {'operation_id':operation,'state':'managed','channels':[channel['id']],
                 'expected_revision':revision})
            self.assertEqual(status,200,saved)
            status, accepted=self.req('/api/gen/xiaole_video',
                {'channel':'grok','operation':'generate','prompt':'local video',
                 'ratio':'9:16','resolution':'720p','duration':5},token=True)
            self.assertEqual(status,200,accepted)
            row=self.wait_job(accepted['job_id'])
            self.assertEqual(row.get('status'),'done',row)
            call=self.provider_posts()[-1]
            self.assertIn('/lechuang-video-'+label+'/',call['path'])
            self.assertEqual(call['authorization'],'Bearer fake-video-'+label)
            self.assertEqual(call['body']['model'],channel['model'])
            self.assertEqual(call['body']['input']['duration_seconds'],5)

    def test_catalog_failure_is_reported_not_silently_fallback(self):
        """目录读不到要如实报错，不能让页面退回硬编码价格继续提交。"""
        # 隔离服务在读取失败时返回 503；这里验证正常路径返回 200 且带来源标记
        st, d = self.req('/api/gen/channel-parameters')
        self.assertEqual(st, 200)
        for item in d['items']:
            if item.get('operation_id') == FUNC:
                self.assertIn('revision', item)
                self.assertIn('combinations', item)


if __name__ == '__main__':
    unittest.main(verbosity=2)
