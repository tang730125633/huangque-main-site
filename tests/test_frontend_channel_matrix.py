import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))

from server.content_domains import frontend_channel_matrix as matrix


class FrontendChannelMatrixTests(unittest.TestCase):
    def setUp(self):
        self.channel = {
            'id': 'managed-image', 'name': '乐创图片主线', 'supplier': '乐创',
            'adapter': 'lechuang_image', 'model': 'gpt-image-2.5-flare',
            'base_url': 'https://api.lechuang.chat/api/v1', 'connection_type': 'relay',
            'configured': True, 'enabled': True,
            'checks': [
                {'kind': 'auth', 'state': 'passed', 'updated': 990},
                {'kind': 'full', 'state': 'passed', 'updated': 995},
            ],
        }
        self.workspace = {
            'items': [self.channel], 'operation_mappings': [],
            'mappings': [{'kind': 'image', 'front': 'gpt-image-2.5-flare',
                          'label': 'GPT Image 2.5', 'channel': 'managed-image',
                          'backup': '', 'enabled': True}],
            'legacy_controls': {},
        }
        self.keys = [
            {'key': 'gemini', 'name': 'Google Gemini API', 'configured': True,
             'required_env': ['GEMINI_API_KEY'],
             'image_primary_base_host': 'generativelanguage.googleapis.com',
             'image_fallback_base_host': 'gemini-relay.example.com',
             'image_probe_base_host': 'gemini-relay.example.com'},
            {'key': 'openai', 'name': 'OpenAI API', 'configured': True,
             'required_env': ['OPENAI_API_KEY'],
             'image_primary_base_host': 'api.openai.com',
             'image_fallback_base_host': 'relay.example.com',
             'image_probe_base_host': 'relay.example.com'},
            {'key': 'seedance', 'name': '火山方舟 API', 'configured': True,
             'required_env': ['ARK_API_KEY'],
             'image_primary_base_host': 'ark.cn-beijing.volces.com',
             'image_probe_base_host': 'ark.cn-beijing.volces.com'},
            {'key': 'xiaolevideo', 'name': '小乐视频 API', 'configured': True,
             'required_env': ['XIAOLEVIDEO_API_KEY'],
             'image_primary_base_host': 'api.xiaolevideo.cn',
             'image_probe_base_host': 'api.xiaolevideo.cn',
             'accepts_new_jobs': False, 'image_accepts_new_jobs': True},
        ]
        order = ['gpt', 'banana', 'seedream', 'lechuang', 'xiaole', 'zelong2']
        self.layout = {
            'layout': {'image': {'order': order, 'default': 'gpt'}},
            'entries': {'image': [
                {'key': key, 'label': key, 'visible': key not in {'xiaole', 'zelong2'},
                 'models': ['GPT Image 2.5'] if key == 'lechuang' else [],
                 'reason': '主站显示' if key not in {'xiaole', 'zelong2'} else '用户页隐藏'}
                for key in order
            ]},
        }
        self.features = [
            {'key': 'image', 'enabled': True}, {'key': 'banana', 'enabled': True},
            {'key': 'image_xiaole', 'enabled': True},
        ]

    def build(self):
        return matrix.build(
            self.workspace, self.keys,
            {'openai': {'status': 'auth_ok', 'checked_at': 998},
             'gemini': {'status': 'auth_ok', 'checked_at': 998}},
            self.layout, self.features, now=1000,
        )

    def test_groups_frontend_products_into_model_tiers(self):
        result = self.build()
        products = {item['key']: item for item in result['products']}
        self.assertEqual([x['label'] for x in products['banana']['models']],
                         ['纳米香蕉 2', '纳米香蕉 Pro'])
        self.assertEqual(products['banana']['models'][0]['actual_model'],
                         'gemini-3.1-flash-image')
        self.assertEqual([x['label'] for x in products['seedream']['models']],
                         ['标准版', 'Pro 版'])
        self.assertEqual(len(products['openai']['models']), 1)
        self.assertEqual(products['openai']['models'][0]['actual_model'], 'gpt-image-2')

    def test_reports_real_managed_and_legacy_channels_without_secrets(self):
        result = self.build()
        products = {item['key']: item for item in result['products']}
        openai = products['openai']['models'][0]['routes'][0]['primary']
        self.assertEqual(openai['name'], 'OpenAI API')
        self.assertEqual(openai['connection_type'], 'official')
        self.assertEqual(openai['credential_source'], '服务器环境变量 · OPENAI_API_KEY')
        self.assertEqual(products['openai']['models'][0]['routes'][0]['backup']['base_host'],
                         'relay.example.com')
        self.assertEqual(openai['auth']['state'], 'unverified')
        self.assertEqual(products['openai']['models'][0]['routes'][0]['backup']['auth']['state'],
                         'ok')
        banana_route = products['banana']['models'][0]['routes'][0]
        self.assertEqual(banana_route['primary']['base_host'],
                         'generativelanguage.googleapis.com')
        self.assertEqual(banana_route['primary']['connection_type'], 'official')
        self.assertEqual(banana_route['primary']['auth']['state'], 'unverified')
        self.assertEqual(banana_route['backup']['base_host'], 'gemini-relay.example.com')
        self.assertEqual(banana_route['backup']['connection_type'], 'relay')
        self.assertEqual(banana_route['backup']['auth']['state'], 'ok')
        lechuang = products['lechuang']['models'][0]
        self.assertEqual(lechuang['actual_model'], 'gpt-image-2.5-flare')
        self.assertEqual(lechuang['routes'][0]['primary']['connection_type'], 'relay')
        self.assertNotIn('secret', json.dumps(result).lower())

    def test_operation_mapping_can_split_one_model_by_capability(self):
        self.workspace['operation_mappings'] = [{
            'operation_id': 'image.banana.nb2.text', 'state': 'managed',
            'channel': 'managed-image', 'backup': '',
        }]
        result = self.build()
        banana = next(x for x in result['products'] if x['key'] == 'banana')
        nb2 = next(x for x in banana['models'] if x['key'] == 'nb2')
        self.assertEqual([x['control_state'] for x in nb2['routes']], ['managed', 'legacy'])
        self.assertEqual(nb2['routes'][0]['primary']['name'], '乐创图片主线')
        self.assertEqual(nb2['routes'][1]['primary']['name'], 'Google Gemini API')

    def test_xiaole_follows_feature_flag_and_zelong_remains_retired(self):
        result = self.build()
        products = {item['key']: item for item in result['products']}
        self.assertTrue(products['xiaole']['admitted'])
        self.assertTrue(products['xiaole']['models'][0]['admitted'])
        self.assertEqual(products['xiaole']['warning'], '')
        self.assertFalse(products['zelong2']['admitted'])

        self.features[-1]['enabled'] = False
        products = {item['key']: item for item in self.build()['products']}
        self.assertFalse(products['xiaole']['admitted'])
        self.assertIn('功能开关未开启', products['xiaole']['models'][0]['reason'])

    def test_lechuang_requires_published_frontend_parameter_entry(self):
        lechuang_entry = next(
            item for item in self.layout['entries']['image'] if item['key'] == 'lechuang'
        )
        lechuang_entry['models'] = []
        product = next(item for item in self.build()['products'] if item['key'] == 'lechuang')
        self.assertEqual(product['models'], [])
        self.assertFalse(product['admitted'])
        self.assertIn('尚无已发布', product['warning'])

    def test_official_host_with_standard_port_is_not_mislabeled_as_relay(self):
        self.assertEqual(matrix._transport('api.openai.com:443'), 'official')


if __name__ == '__main__':
    unittest.main()
