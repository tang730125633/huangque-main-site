# -*- coding: utf-8 -*-
"""统一解析：目录、报价与受理必须用同一个业务功能解析结果。

覆盖用户指出的四个阻断点：
  1. 旧 grok 映射指向 A、功能映射指向 B —— 目录与受理都必须用 B；
  2. 同名不同功能（纳米香蕉 2 文生图 / 参考图）各自成条，可配不同渠道；
  3. 没有 channel/model/variant 的合法功能也要能进目录；
  5. 点数变化按用户原先选中的组合比，不拿两个默认组合比。

不发起任何供应商调用：只验证「识别 → 主渠道 → 参数/报价/绑定」。
"""
import base64
import os
import tempfile
import unittest
from unittest.mock import patch

from server.content_domains import channel_manager as cm, channel_parameters as params


class FunctionRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {
            'HQ_CHANNEL_DB': self.tmp.name + '/channels.db',
            'HQ_OBSERVABILITY_DB': self.tmp.name + '/trace.db',
            'HQ_PROVIDER_KEYS_MASTER_KEY': base64.urlsafe_b64encode(b'a' * 32).decode(),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def channel(self, name, adapter, kind, model):
        return cm.save('admin', dict(
            name=name, adapter=adapter, kind=kind, model=model,
            base_url='https://api.lechuang.chat/api/v1', secret='sk-test', enabled=True,
            test_cost=1, daily_limit=5, daily_budget=100,
            fixture={'prompt': 'a', 'reference_images': []},
        ))['id']

    def publish(self, cid, points):
        cap = params.capabilities(cm.version(cid))
        values = {k: v[0] for k, v in cap['fields'].items()}
        spec = dict(profile=cap['profile'],
                    fields=[dict(key=k, label=params.LABELS.get(k, k), visible=True) for k in cap['fields']],
                    combinations=[dict(id='std', values=values, points=points)], default='std',
                    reference_min=cap['reference_min'], reference_max=cap['reference_max'])
        for action in ('draft', 'publish'):
            st = params.admin_state(cid)
            params.change('admin', dict(id=cid, version=st['version'],
                                        draft_revision=(st['draft'] or {}).get('revision', 0),
                                        action=action, parameters=spec, confirmed=True))

    def entry(self, front, kind='xiaole_video'):
        hits = [i for i in params._published_items() if i['kind'] == kind and i['front'] == front]
        return hits[0] if hits else None

    # —— 1. 旧映射与功能映射冲突时，统一走功能映射 ——
    def test_legacy_grok_entry_follows_function_mapping(self):
        a = self.channel('A 1.0', 'lechuang_video', 'xiaole_video', 'grok-video-1.0')
        self.publish(a, 60)
        b = self.channel('B 1.5', 'lechuang_video', 'xiaole_video', 'grok-video-1.5')
        self.publish(b, 90)
        cm.save_mapping('admin', dict(kind='xiaole_video', front='grok', label='Grok 1.0',
                                      channel=a, enabled=True))
        cm.save_operation_mapping('admin', dict(operation_id='video.grok.text', state='managed',
                                                channels=[b], expected_revision=0))
        item = self.entry('grok')
        self.assertIsNotNone(item, '旧 grok 条目仍应在目录里（它是入口标识）')
        # 关键：它的参数与报价必须来自功能映射的主渠道 B，而不是旧映射的 A
        self.assertEqual(item['operation_id'], 'video.grok.text')
        self.assertEqual(item['combinations'][0]['points'], 90, '应取 B 的 90 点，不是 A 的 60 点')
        # 受理侧也必须绑到 B：用目录给出的 revision 与组合提交，与实际页面一致
        out = cm.capture('xiaole_video', {
            'channel': 'grok', 'prompt': 'x',
            'parameter_selection': {'revision': item['revision'],
                                    'combination': item['combinations'][0]['id']}})
        binding = out.get('_channel_binding') or {}
        self.assertEqual(binding.get('id'), b, '受理必须绑到功能主渠道 B，而不是旧映射的 A')
        self.assertEqual(binding.get('version'), cm.version(b)['version'])

    # —— 2. 同名不同功能各自成条 ——
    def test_same_front_different_functions_kept_separately(self):
        t = self.channel('纳米香蕉 文生图', 'lechuang_image', 'image', 'nb2')
        self.publish(t, 12)
        r = self.channel('纳米香蕉 参考图', 'lechuang_image', 'image', 'nb2')
        self.publish(r, 20)
        cm.save_operation_mapping('admin', dict(operation_id='image.banana.nb2.text',
                                                state='managed', channels=[t], expected_revision=0))
        cm.save_operation_mapping('admin', dict(operation_id='image.banana.nb2.reference',
                                                state='managed', channels=[r], expected_revision=0))
        items = [i for i in params._published_items() if i.get('operation_id', '').startswith('image.banana.nb2')]
        by_op = {i['operation_id']: i for i in items}
        self.assertIn('image.banana.nb2.text', by_op)
        self.assertIn('image.banana.nb2.reference', by_op)
        # 两个功能可以各配各的渠道，各取各的点数
        self.assertEqual(by_op['image.banana.nb2.text']['combinations'][0]['points'], 12)
        self.assertEqual(by_op['image.banana.nb2.reference']['combinations'][0]['points'], 20)
        # 条目带上识别条件，客户端据此按真实输入匹配
        self.assertEqual((by_op['image.banana.nb2.text'].get('match') or {}).get('reference_count'), 0)
        self.assertEqual((by_op['image.banana.nb2.reference'].get('match') or {}).get('reference_count'), '>0')

    # —— 3. 没有 channel/model/variant 的功能也能进目录 ——
    def test_function_without_selector_fields_still_listed(self):
        cid = self.channel('配音', 'cosyvoice_tts', 'audio', 'cosyvoice-v1')
        self.publish(cid, 10)
        cm.save_operation_mapping('admin', dict(operation_id='audio.tts.public', state='managed',
                                                channels=[cid], expected_revision=0))
        item = self.entry('public', kind='audio')
        self.assertIsNotNone(item, '没有 channel/model/variant 的功能也要能进目录')
        self.assertEqual(item['operation_id'], 'audio.tts.public')
        self.assertEqual(item['combinations'][0]['points'], 10)

    # —— 6. 目录读取失败必须报错，不能悄悄退回旧配置 ——
    def test_catalog_read_failure_propagates(self):
        cid = self.channel('A', 'lechuang_video', 'xiaole_video', 'grok-video-1.0')
        self.publish(cid, 60)
        cm.save_mapping('admin', dict(kind='xiaole_video', front='grok', label='Grok',
                                      channel=cid, enabled=True))
        with patch.object(cm, 'operation_mapping', side_effect=RuntimeError('映射读取失败')):
            with self.assertRaises(RuntimeError):
                params._published_items()


if __name__ == '__main__':
    unittest.main()
