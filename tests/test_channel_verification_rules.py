# -*- coding: utf-8 -*-
"""验证规则传递：判定必需项 vs 协议支持项，以及拒绝不适用的检测类型。

背景：HeyGen MCP 协议只以完整生成为证据，但列表里留下了一条不适用的
HEAD 连接失败记录，把「能用」的渠道判成了异常。修复后：
  1. 规则随每条渠道一起下发，前端不再靠缺字段回退成「要求三项」；
  2. 协议不支持的检测类型后端直接拒绝，不产生误导性的失败记录；
  3. 历史的不适用记录保留、仍然展示，只是不参与总体判定。
"""
import base64
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from server.content_domains import channel_manager as cm, channel_runtime as runtime


class RuleDeclarationTests(unittest.TestCase):
    def test_required_and_supported_are_separate(self):
        self.assertEqual(cm.verification_required('heygen_mcp_video'), ('full',))
        self.assertEqual(cm.checks_supported('heygen_mcp_video'), ('full',))
        # 不是「判定必需」不等于「协议不支持」——minimax_h3/sora_video 只要求两项，
        # 但三项探测都能执行，不能因为非必需就当成不支持。
        self.assertEqual(cm.verification_required('sora_video'), ('connection', 'full'))
        self.assertEqual(cm.checks_supported('sora_video'), ('connection', 'auth', 'full'))

    def test_unknown_adapter_does_not_default_to_pass(self):
        # 未知协议按三项要求算，不因为缺声明而放行
        self.assertEqual(cm.verification_required('nope'), ('connection', 'auth', 'full'))
        self.assertEqual(cm.checks_supported('nope'), ('connection', 'auth', 'full'))


class StartTestRejectionTests(unittest.TestCase):
    def setUp(self):
        # 提交验证会起后台守护线程执行任务，Windows 上临时库文件可能仍被它打开。
        # 这是夹具与守护线程的清理竞态，不是被测行为；显式忽略清理错误，
        # 但断言本身照常严格执行。
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {
            'HQ_CHANNEL_DB': self.tmp.name + '/channels.db',
            'HQ_OBSERVABILITY_DB': self.tmp.name + '/trace.db',
            'HQ_PROVIDER_KEYS_MASTER_KEY': base64.urlsafe_b64encode(b'a' * 32).decode(),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def channel(self, adapter, kind):
        return cm.save('tester', dict(
            name='规则测试渠道', adapter=adapter, kind=kind, model='m',
            base_url='https://api.lechuang.chat/api/v1', secret='sk-test', enabled=True,
            fixture={'prompt': 'a', 'reference_images': []},
        ))['id']

    def test_unsupported_kind_is_rejected_with_reason(self):
        cid = self.channel('heygen_mcp_video', 'video')
        for kind in ('connection', 'auth'):
            with self.assertRaises(ValueError) as ctx:
                runtime.start_test('tester', {'id': cid, 'kind': kind})
            self.assertIn('不支持', str(ctx.exception))
            self.assertIn('完整生成', str(ctx.exception))

    def test_unsupported_kind_leaves_no_run_record(self):
        cid = self.channel('heygen_mcp_video', 'video')
        with self.assertRaises(ValueError):
            runtime.start_test('tester', {'id': cid, 'kind': 'auth'})
        # 没有落库 → 不会出现一条误导性的失败记录
        self.assertEqual([r for r in cm.overview()['items'] if r['id'] == cid][0]['checks'], [])

    def test_supported_kind_is_accepted_and_returns_tracking_fields(self):
        cid = self.channel('lechuang_image', 'image')
        res = runtime.start_test('tester', {'id': cid, 'kind': 'connection'})
        for field in ('run_id', 'state', 'channel', 'version', 'kind'):
            self.assertIn(field, res)
        self.assertEqual(res['channel'], cid)
        self.assertEqual(res['state'], 'queued')

    def test_run_state_reports_progress_by_run_id(self):
        cid = self.channel('lechuang_image', 'image')
        rid = runtime.start_test('tester', {'id': cid, 'kind': 'connection'})['run_id']
        st = runtime.run_state('tester', {'run_id': rid})
        self.assertEqual(st['run_id'], rid)
        self.assertEqual(st['channel'], cid)
        self.assertIn(st['state'], ('queued', 'running', 'passed', 'failed', 'unknown'))
        self.assertIn('label', st)

    def test_run_state_rejects_missing_or_unknown_id(self):
        with self.assertRaises(ValueError):
            runtime.run_state('tester', {})
        with self.assertRaises(ValueError):
            runtime.run_state('tester', {'run_id': 'no-such-run'})


class OverviewCarriesRulesTests(unittest.TestCase):
    def setUp(self):
        # 提交验证会起后台守护线程执行任务，Windows 上临时库文件可能仍被它打开。
        # 这是夹具与守护线程的清理竞态，不是被测行为；显式忽略清理错误，
        # 但断言本身照常严格执行。
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {
            'HQ_CHANNEL_DB': self.tmp.name + '/channels.db',
            'HQ_OBSERVABILITY_DB': self.tmp.name + '/trace.db',
            'HQ_PROVIDER_KEYS_MASTER_KEY': base64.urlsafe_b64encode(b'a' * 32).decode(),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_items_carry_verification_rules(self):
        cid = cm.save('tester', dict(
            name='HeyGen 规则下发', adapter='heygen_mcp_video', kind='video', model='m',
            base_url='https://api.lechuang.chat/api/v1', secret='sk-test', enabled=True,
            fixture={'prompt': 'a', 'reference_images': []},
        ))['id']
        item = [r for r in cm.overview()['items'] if r['id'] == cid][0]
        # 前端靠这两个字段判定，不再回退成「要求三项」
        self.assertEqual(item['verification'], ['full'])
        self.assertEqual(item['checks_supported'], ['full'])
        self.assertTrue(item['rules_known'])


if __name__ == '__main__':
    unittest.main()


class SubmitIdempotencyTests(unittest.TestCase):
    """验证提交去重：重复点击、响应丢失后重试都不该重复创建（完整生成是收费的）。"""

    def setUp(self):
        # 提交验证会起后台守护线程执行任务，Windows 上临时库文件可能仍被它打开。
        # 这是夹具与守护线程的清理竞态，不是被测行为；显式忽略清理错误，
        # 但断言本身照常严格执行。
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {
            'HQ_CHANNEL_DB': self.tmp.name + '/channels.db',
            'HQ_OBSERVABILITY_DB': self.tmp.name + '/trace.db',
            'HQ_PROVIDER_KEYS_MASTER_KEY': base64.urlsafe_b64encode(b'a' * 32).decode(),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cid = cm.save('tester', dict(
            name='去重测试', adapter='lechuang_image', kind='image', model='m',
            base_url='https://api.lechuang.chat/api/v1', secret='sk-test', enabled=True,
            fixture={'prompt': 'a', 'reference_images': []},
        ))['id']

    def run_count(self, kind):
        with __import__('contextlib').closing(cm.db()) as c:
            return c.execute('SELECT COUNT(*) FROM runs WHERE channel=? AND kind=?', (self.cid, kind)).fetchone()[0]

    def test_repeated_submit_reuses_one_run(self):
        first = runtime.start_test('tester', {'id': self.cid, 'kind': 'connection'})
        for _ in range(3):
            again = runtime.start_test('tester', {'id': self.cid, 'kind': 'connection'})
            self.assertEqual(again['run_id'], first['run_id'])
            self.assertTrue(again.get('deduplicated'))
        self.assertEqual(self.run_count('connection'), 1)

    def test_different_kind_is_not_deduplicated(self):
        a = runtime.start_test('tester', {'id': self.cid, 'kind': 'connection'})
        b = runtime.start_test('tester', {'id': self.cid, 'kind': 'auth'})
        self.assertNotEqual(a['run_id'], b['run_id'])

    def test_changed_version_is_not_deduplicated(self):
        a = runtime.start_test('tester', {'id': self.cid, 'kind': 'connection'})
        cur = [r for r in cm.overview()['items'] if r['id'] == self.cid][0]['version']
        cm.save('tester', dict(id=self.cid, name='去重测试', adapter='lechuang_image', kind='image',
                               model='m2', base_url='https://api.lechuang.chat/api/v1',
                               secret='sk-test', enabled=True, version=cur,
                               fixture={'prompt': 'a', 'reference_images': []}))
        b = runtime.start_test('tester', {'id': self.cid, 'kind': 'connection'})
        self.assertNotEqual(a['run_id'], b['run_id'])


class RunStateSafetyTests(unittest.TestCase):
    """⑦ run_state 保留管理员鉴权，且不暴露密钥。"""

    def setUp(self):
        # 提交验证会起后台守护线程执行任务，Windows 上临时库文件可能仍被它打开。
        # 这是夹具与守护线程的清理竞态，不是被测行为；显式忽略清理错误，
        # 但断言本身照常严格执行。
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {
            'HQ_CHANNEL_DB': self.tmp.name + '/channels.db',
            'HQ_OBSERVABILITY_DB': self.tmp.name + '/trace.db',
            'HQ_PROVIDER_KEYS_MASTER_KEY': base64.urlsafe_b64encode(b'a' * 32).decode(),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_route_is_inside_authenticated_admin_branch(self):
        import io as _io
        with _io.open(os.path.join(os.path.dirname(__file__), '..', 'server', 'admin_api.py'),
                      encoding='utf-8') as fh:
            src = fh.read()
        guard = src.index("startswith('/api/admin/channel-manager/')")
        route = src.index("'run-state':channel_runtime.run_state")
        self.assertGreater(route, guard, 'run-state 必须在管理员鉴权分支内注册')

    def test_response_never_carries_secrets(self):
        cid = cm.save('tester', dict(
            name='密钥不外泄', adapter='lechuang_image', kind='image', model='m',
            base_url='https://api.lechuang.chat/api/v1', secret='sk-super-secret-value',
            enabled=True, fixture={'prompt': 'a', 'reference_images': []},
        ))['id']
        rid = runtime.start_test('tester', {'id': cid, 'kind': 'connection'})['run_id']
        blob = json.dumps(runtime.run_state('tester', {'run_id': rid}), ensure_ascii=False, default=str)
        self.assertNotIn('sk-super-secret-value', blob)
        for key in ('secret', 'api_key', 'apikey', 'token', 'password'):
            self.assertNotIn('"%s"' % key, blob.lower())


class VersionAttributionTests(unittest.TestCase):
    """⑤ 验证期间修改配置，结果仍归属被测版本。"""

    def setUp(self):
        # 提交验证会起后台守护线程执行任务，Windows 上临时库文件可能仍被它打开。
        # 这是夹具与守护线程的清理竞态，不是被测行为；显式忽略清理错误，
        # 但断言本身照常严格执行。
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {
            'HQ_CHANNEL_DB': self.tmp.name + '/channels.db',
            'HQ_OBSERVABILITY_DB': self.tmp.name + '/trace.db',
            'HQ_PROVIDER_KEYS_MASTER_KEY': base64.urlsafe_b64encode(b'a' * 32).decode(),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.body = dict(name='归属测试', adapter='lechuang_image', kind='image', model='m',
                         base_url='https://api.lechuang.chat/api/v1', secret='sk-test', enabled=True,
                         fixture={'prompt': 'a', 'reference_images': []})

    def test_submitted_version_is_frozen_and_kept_after_config_change(self):
        cid = cm.save('tester', self.body)['id']
        submitted = runtime.start_test('tester', {'id': cid, 'kind': 'connection'})
        self.assertEqual(submitted['version'], 1)
        # 验证进行中修改配置
        cur = [r for r in cm.overview()['items'] if r['id'] == cid][0]['version']
        cm.save('tester', dict(self.body, id=cid, version=cur, model='m2'))
        # 结果仍归属被测版本 1
        st = runtime.run_state('tester', {'run_id': submitted['run_id']})
        self.assertEqual(st['version'], 1)
        # 之后按 v2 提交会是一条新任务，不会复用 v1 那条
        again = runtime.start_test('tester', {'id': cid, 'kind': 'connection'})
        self.assertEqual(again['version'], 2)
        self.assertNotEqual(again['run_id'], submitted['run_id'])

    def test_checks_are_reported_with_their_version(self):
        cid = cm.save('tester', self.body)['id']
        rid = runtime.start_test('tester', {'id': cid, 'kind': 'connection'})['run_id']
        cm.finish(rid, 'passed', '连接可达')
        item = [r for r in cm.overview()['items'] if r['id'] == cid][0]
        row = [c for c in item['checks'] if c['kind'] == 'connection'][0]
        # 前端靠 version 归属，缺了就会显示「验证记录未标注配置版本」
        self.assertEqual(row['version'], item['version'])
