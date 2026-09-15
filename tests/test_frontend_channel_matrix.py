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
             'image_probe_base_host': 'gemini-relay.example.com',
             'image_primary_active': True},
            {'key': 'openai', 'name': 'OpenAI API', 'configured': True,
             'required_env': ['OPENAI_API_KEY'],
             'image_primary_base_host': 'api.openai.com',
             'image_fallback_base_host': 'relay.example.com',
             'image_probe_base_host': 'relay.example.com',
             'image_primary_active': True},
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
            'layout': {
                'image': {'order': order, 'default': 'gpt'},
                'video': {'order': ['grok', 'talking', 'cinematic', 'tryon',
                                    'minimax', 'micro', 'sora', 'omni'],
                          'default': 'grok'},
            },
            'entries': {'image': [
                {'key': key, 'label': key, 'visible': key not in {'xiaole', 'zelong2'},
                 'models': ['GPT Image 2.5'] if key == 'lechuang' else [],
                 'model_keys': ['gpt-image-2.5-flare'] if key == 'lechuang' else [],
                 'reason': '主站显示' if key not in {'xiaole', 'zelong2'} else '用户页隐藏'}
                for key in order
            ], 'video': [
                {'key': key, 'label': label, 'visible': True, 'reason': '主站显示'}
                for key, label in [
                    ('grok', '果肉视频生成'), ('talking', '数字化 IP'),
                    ('cinematic', '电影化身'), ('tryon', '换装换背景'),
                    ('minimax', '麦克视频'), ('micro', 'Seedance 视频'),
                    ('sora', 'Sora 2'), ('omni', 'Omni 视频'),
                ]
            ]},
        }
        self.features = [
            {'key': 'image', 'enabled': True}, {'key': 'banana', 'enabled': True},
            {'key': 'image_xiaole', 'enabled': True},
        ] + [
            {'key': key, 'enabled': True}
            for key in ['video', 'cinematic', 'tryon', 'grok_video', 'sora_video',
                        'minimax_h3_video', 'omni_video', 'seedance_video']
        ]
        self.provider_keys = [
            {'id': provider + '-1', 'provider': provider, 'label': provider + ' 主线',
             'base_url': base, 'state': 'active', 'health_status': 'healthy',
             'last_checked_at': 999}
            for provider, base in [
                ('xai', 'https://api.x.ai/v1'), ('sora', 'https://api.openai.com'),
                ('seedance', 'https://ark.cn-beijing.volces.com/api/v3'),
                ('omni', 'https://generativelanguage.googleapis.com'),
                ('minimax', 'https://metaso.cn/api/minimax'),
            ]
        ]
        self.runtime_health = {
            'sora_video_enabled': True,
            'minimax_h3_video_enabled': True,
            'omni_video_enabled': True,
            'seedance_video_enabled': True,
        }

    def build(self, now=1000):
        return matrix.build(
            self.workspace, self.keys,
            {'openai': {'status': 'auth_ok', 'checked_at': 998},
             'gemini': {'status': 'auth_ok', 'checked_at': 998}},
            self.layout, self.features, now=now,
            provider_key_rows=self.provider_keys,
            runtime_health=self.runtime_health,
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

        next(item for item in self.features if item['key'] == 'image_xiaole')['enabled'] = False
        products = {item['key']: item for item in self.build()['products']}
        self.assertFalse(products['xiaole']['admitted'])
        self.assertIn('功能开关未开启', products['xiaole']['models'][0]['reason'])

    def test_lechuang_requires_published_frontend_parameter_entry(self):
        lechuang_entry = next(
            item for item in self.layout['entries']['image'] if item['key'] == 'lechuang'
        )
        lechuang_entry['model_keys'] = []
        product = next(item for item in self.build()['products'] if item['key'] == 'lechuang')
        self.assertEqual(product['models'], [])
        self.assertFalse(product['admitted'])
        self.assertIn('尚无已发布', product['warning'])

    def test_lechuang_uses_stable_front_key_when_labels_collide(self):
        self.workspace['items'].append(dict(
            self.channel, id='unpublished-image', model='unpublished-model'
        ))
        self.workspace['mappings'].append({
            'kind': 'image', 'front': 'unpublished-front', 'label': 'GPT Image 2.5',
            'channel': 'unpublished-image', 'backup': '', 'enabled': True,
        })
        product = next(item for item in self.build()['products'] if item['key'] == 'lechuang')
        self.assertEqual([item['key'] for item in product['models']], ['gpt-image-2.5-flare'])

    def test_no_egress_proxy_promotes_relay_to_the_only_primary(self):
        for key in ('openai', 'gemini'):
            next(item for item in self.keys if item['key'] == key)['image_primary_active'] = False
        products = {item['key']: item for item in self.build()['products']}
        openai = products['openai']['models'][0]['routes'][0]
        banana = products['banana']['models'][0]['routes'][0]
        self.assertEqual(openai['primary']['base_host'], 'relay.example.com')
        self.assertIsNone(openai['backup'])
        self.assertEqual(banana['primary']['base_host'], 'gemini-relay.example.com')
        self.assertIsNone(banana['backup'])

    def test_official_host_with_standard_port_is_not_mislabeled_as_relay(self):
        self.assertEqual(matrix._transport('api.openai.com:443'), 'official')

    def test_video_page_matches_frontend_products_models_and_pool_routes(self):
        result = self.build()
        video = next(page for page in result['pages'] if page['page'] == 'video')
        products = {item['key']: item for item in video['products']}
        self.assertEqual(len(products), 8)
        self.assertEqual([x['label'] for x in products['grok']['models']],
                         ['通用版 · 文生/图生', '1.5 · 高质量图生'])
        self.assertEqual([x['actual_model'] for x in products['sora']['models']],
                         ['sora-2', 'sora-2-pro'])
        self.assertEqual(
            products['grok']['models'][0]['routes'][0]['primary']['credential_source'],
            '后台密钥号池 · 1 个密钥，1 个可轮转',
        )
        self.assertEqual(
            products['grok']['models'][0]['routes'][0]['primary']['pool_usable'],
            1,
        )
        self.assertEqual(
            products['grok']['models'][0]['routes'][0]['primary']['connection_type'],
            'official',
        )
        self.assertEqual(
            products['tryon']['models'][0]['routes'][0]['primary']['base_host'],
            'api.wavespeed.ai',
        )
        self.assertFalse(products['seedance']['models'][1]['admitted'])
        self.assertIn('前台当前未开放', products['seedance']['models'][1]['reason'])

    def test_video_pool_health_and_operation_mapping_stay_secret_free(self):
        self.provider_keys[0]['health_status'] = 'unhealthy'
        self.workspace['operation_mappings'] = [{
            'operation_id': 'video.sora.text', 'state': 'managed',
            'channel': 'managed-image', 'backup': '',
        }]
        video = next(page for page in self.build()['pages'] if page['page'] == 'video')
        products = {item['key']: item for item in video['products']}
        grok = products['grok']['models'][0]['routes'][0]['primary']
        self.assertFalse(grok['enabled'])
        sora = products['sora']['models'][0]['routes'][0]
        self.assertEqual(sora['control_state'], 'managed')
        self.assertEqual(sora['primary']['name'], '乐创图片主线')
        self.assertNotIn('last4', json.dumps(video).lower())

    def test_env_only_compatibility_key_is_not_a_paid_runtime_candidate(self):
        self.provider_keys = [{
            'id': 'env', 'provider': 'xai', 'label': '环境兼容线路',
            'base_url': 'https://api.x.ai/v1', 'state': 'active',
            'health_status': 'unknown', 'managed': False,
        }]
        video = next(page for page in self.build()['pages'] if page['page'] == 'video')
        grok = next(item for item in video['products'] if item['key'] == 'grok')
        route = grok['models'][0]['routes'][0]
        self.assertFalse(route['primary']['configured'])
        self.assertFalse(route['primary']['enabled'])
        self.assertFalse(route['admitted'])
        self.assertFalse(grok['admitted'])

    def test_pool_auth_evidence_expires_and_uses_the_healthy_key_timestamp(self):
        self.provider_keys[0]['last_checked_at'] = 100
        self.provider_keys.append({
            'id': 'xai-bad', 'provider': 'xai', 'label': '较新失败线路',
            'base_url': 'https://api.x.ai/v1', 'state': 'active',
            'health_status': 'unhealthy', 'last_checked_at': 99999,
        })
        video = next(page for page in self.build(now=90001)['pages'] if page['page'] == 'video')
        route = next(item for item in video['products'] if item['key'] == 'grok')['models'][0]['routes'][0]
        self.assertEqual(route['primary']['auth']['state'], 'stale')
        self.assertEqual(route['primary']['auth']['checked_at'], 100)

    def test_video_admission_requires_configured_credentials(self):
        video = next(page for page in self.build()['pages'] if page['page'] == 'video')
        talking = next(item for item in video['products'] if item['key'] == 'digital_ip')
        self.assertFalse(talking['admitted'])
        self.assertTrue(all(not route['admitted'] for route in talking['models'][0]['routes']))

    def test_video_visibility_follows_the_same_runtime_health_as_the_frontend(self):
        self.runtime_health['sora_video_enabled'] = False
        self.runtime_health['omni_video_enabled'] = False
        video = next(page for page in self.build()['pages'] if page['page'] == 'video')
        products = {item['key']: item for item in video['products']}
        for key in ('sora', 'omni'):
            self.assertFalse(products[key]['visible'])
            self.assertFalse(products[key]['admitted'])
            self.assertIn('前台运行时当前未开放', products[key]['visibility_reason'])


if __name__ == '__main__':
    unittest.main()
