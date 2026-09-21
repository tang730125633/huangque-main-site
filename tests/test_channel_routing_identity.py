# -*- coding: utf-8 -*-
"""功能身份与映射解析：缺陷复现材料（修复后改成正确行为的回归测试）。

准确的缺陷形态（已按真实入口核对）：

  1. 参数目录 _published_items() 的数据源是【旧线路映射】mappings（kind:front），
     而管理员配的是【功能映射】operation_mappings。两套数据源。
  2. 旧 mappings 里只有乐创系 front（gpt-image-2 / gpt-image-2.5-flare）与
     grok / grok15；纳米香蕉 2、黄雀引擎 1 这些**有功能映射的功能没有目录项**，
     所以它们的参数与报价取不到。
  3. 功能识别本身是对的：纳米香蕉 2 经 /api/gen/banana（服务端补
     provider="banana"）正确落到 image.banana.nb2.text —— 问题在目录侧，
     不在识别侧。修的时候不要动识别。
  4. 页面在目录里选不到目标条目时会静默回落到 items[0]
     （channel-parameters.js：items.find(...)||items[0]）。

不成立、不要再当缺陷处理的：
  * 「纳米香蕉配 B、但用户在乐创 Image 2 入口提交却走了 A」——
    两个入口本来就是不同业务功能，各用各的渠道是正确行为。
  * 「图片缺 source_page 必然走原厂」——缺 source_page 会绕过功能识别，
    但仍可能命中旧托管映射。

本文件只做「服务端补全 → 功能识别 → 映射 → 渠道」的判定，不发起任何供应商调用。
"""
import base64
import os
import tempfile
import unittest
from unittest.mock import patch

from server.content_domains import channel_manager as cm, channel_parameters as params
from server.content_domains.function_registry import classify_task, operation_catalog

# 业务功能 → 用户真实入口（服务端补全后的字段）
ENTRIES = [
    # 名称（按引擎/功能命名，不用页面文件名）, kind, 服务端补全后的请求体
    ('纳米香蕉 2 文生图', 'image',
     {'source_page': 'banana', 'provider': 'banana', 'model': 'nb2', 'quality': 'std', 'count': 1}),
    ('纳米香蕉 2 参考图', 'image',
     {'source_page': 'banana', 'provider': 'banana', 'model': 'nb2', 'reference_count': 1}),
    ('纳米香蕉 Pro 文生图', 'image',
     {'source_page': 'banana', 'provider': 'banana', 'model': 'pro', 'quality': 'std', 'count': 1}),
    ('黄雀引擎 1 标准版文生图', 'image',
     {'source_page': 'banana', 'provider': 'seedream', 'variant': 'std', 'reference_count': 0}),
    ('乐创 GPT Image 2 文生图', 'image',
     {'source_page': 'banana', 'provider': 'openai', 'model': 'gpt-image-2', 'reference_count': 0}),
    ('乐创 GPT Image 2.5 文生图', 'image',
     {'source_page': 'banana', 'provider': 'openai', 'model': 'gpt-image-2.5-flare', 'reference_count': 0}),
    ('果肉文生视频', 'xiaole_video', {'channel': 'grok', 'operation': 'generate'}),
    ('果肉参考图生视频', 'xiaole_video', {'channel': 'grok', 'operation': 'generate', 'reference_count': 1}),
]


class EntryIdentityTests(unittest.TestCase):
    """每个业务功能识别到哪个叶子 —— 识别层本身是否正确。"""

    def test_entries_resolve_to_registered_leaves(self):
        known = {o['operation_id'] for o in operation_catalog()}
        got = {name: classify_task(kind, dict(p)) for name, kind, p in ENTRIES}
        # 识别层是正确的：这些都必须落到真实登记的叶子
        self.assertEqual(got['纳米香蕉 2 文生图'], 'image.banana.nb2.text')
        self.assertEqual(got['纳米香蕉 2 参考图'], 'image.banana.nb2.reference')
        self.assertEqual(got['纳米香蕉 Pro 文生图'], 'image.banana.pro.text')
        self.assertEqual(got['黄雀引擎 1 标准版文生图'], 'image.seedream.std.text')
        self.assertEqual(got['乐创 GPT Image 2 文生图'], 'image.openai.text')
        self.assertEqual(got['果肉文生视频'], 'video.grok.text')
        for name, oid in got.items():
            self.assertIsNotNone(oid, '%s 应当能被识别' % name)
            self.assertIn(oid, known, '%s 识别出未登记的叶子 %s' % (name, oid))


class CatalogCoverageTests(unittest.TestCase):
    """缺陷复现：参数目录覆盖不到「有功能映射的功能」。"""

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

    def publish(self, cid, points=18):
        cap = params.capabilities(cm.version(cid))
        values = {k: v[0] for k, v in cap['fields'].items()}
        spec = dict(profile=cap['profile'],
                    fields=[dict(key=k, label=params.LABELS[k], visible=True) for k in cap['fields']],
                    combinations=[dict(id='std', values=values, points=points)], default='std',
                    reference_min=cap['reference_min'], reference_max=cap['reference_max'])
        for action in ('draft', 'publish'):
            st = params.admin_state(cid)
            params.change('admin', dict(id=cid, version=st['version'],
                                        draft_revision=(st['draft'] or {}).get('revision', 0),
                                        action=action, parameters=spec, confirmed=True))

    def test_catalog_covers_mapped_functions(self):
        """修复后：有托管功能映射的功能，即使没有旧映射条目，也要出现在目录里，
        且参数与报价来自【该功能的主渠道】。"""
        lechuang = self.channel('乐创 2.0', 'lechuang_image', 'image', 'gpt-image-2')
        self.publish(lechuang, 18)
        cm.save_mapping('admin', dict(kind='image', front='gpt-image-2', label='GPT Image 2',
                                      channel=lechuang, enabled=True))
        # 纳米香蕉 2 的功能映射存在（模拟线上 r18），但目录里没有它
        banana = self.channel('纳米香蕉渠道', 'lechuang_image', 'image', 'nb2')
        self.publish(banana, 12)
        cm.save_operation_mapping('admin', dict(operation_id='image.banana.nb2.text',
                                                state='managed', channels=[banana], expected_revision=0))
        self.assertIsNotNone(cm.operation_mapping('image.banana.nb2.text'))

        items = params._published_items()
        fronts = {i['front'] for i in items}
        # 旧映射那条照旧在
        self.assertIn('gpt-image-2', fronts)
        # 纳米香蕉 2 这条功能也必须能在目录里选到
        self.assertIn('nb2', fronts)
        banana = [i for i in items if i['front'] == 'nb2'][0]
        # 参数与报价来自它的主渠道（12 点），身份仍是纳米香蕉 2 这个功能
        self.assertEqual(banana['combinations'][0]['points'], 12)
        self.assertEqual(banana.get('operation_id'), 'image.banana.nb2.text')

    def test_legacy_entry_price_still_from_its_own_mapping(self):
        """旧映射条目保持原样：点数仍取自它自己映射的渠道（不回归）。"""
        a = self.channel('A', 'lechuang_image', 'image', 'gpt-image-2')
        self.publish(a, 18)
        b = self.channel('B', 'lechuang_image', 'image', 'gpt-image-2')
        self.publish(b, 35)
        cm.save_mapping('admin', dict(kind='image', front='gpt-image-2', label='x', channel=a, enabled=True))
        item = [i for i in params._published_items() if i['front'] == 'gpt-image-2'][0]
        self.assertEqual(item['combinations'][0]['points'], 18)


class ClientFallbackTests(unittest.TestCase):
    """复现：目录里找不到原选项时，页面静默回落到第一项。"""

    def test_client_falls_back_to_first_item(self):
        import io
        src = io.open(os.path.join(os.path.dirname(__file__), '..', 'site', 'workbench',
                                   'channel-parameters.js'), encoding='utf-8').read()
        # items.find(...)||items[0] —— 找不到就换成第一项，用户没有被告知
        self.assertIn('items.find(i=>i.front===previous?.front)||items[0]', src)

    def test_client_does_not_send_source_page_for_video(self):
        """果肉走 xiaole_video：识别不依赖 source_page，这一条不是缺陷。"""
        self.assertEqual(classify_task('xiaole_video', {'channel': 'grok'}), 'video.grok.text')
        self.assertEqual(classify_task('xiaole_video', {'channel': 'grok', 'source_page': 'video'}),
                         'video.grok.text')


if __name__ == '__main__':
    unittest.main()
