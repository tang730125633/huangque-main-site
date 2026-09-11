import base64
import io
import json
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import tests.test_channel_manager as base
from server.content_domains import channel_manager as cm, channel_parameters as params, channel_runtime as runtime
from server.content_domains import core, safe_http


def _local_resolver(host, port, type=0):
    # 本地沙箱 DNS 会把未知域名解析成保留网段；测试中固定返回公网 IP。
    if host in ('127.0.0.1', 'localhost'):
        return [(2, 1, 6, '', ('127.0.0.1', port))]
    return [(2, 1, 6, '', ('93.184.216.34', port))]


class LechuangAdapterTests(unittest.TestCase):
    def setUp(self):
        base.ChannelTests.setUp(self)
        real_validate = safe_http.validate_target
        self._resolver_patch = patch.object(
            safe_http, 'validate_target',
            side_effect=lambda url, proxy=False: real_validate(url, proxy=proxy, resolver=_local_resolver))
        self._resolver_patch.start()
        self.addCleanup(self._resolver_patch.stop)

    def image_channel(self, model='gpt-image-2'):
        body = dict(self.body, name='乐创生图', adapter='lechuang_image', model=model,
                    base_url='https://api.lechuang.chat/api/v1')
        return cm.save('admin', body)

    def video_channel(self, model='Grok Image Video'):
        body = dict(self.body, name='乐创视频', adapter='lechuang_video', model=model,
                    base_url='https://api.lechuang.chat/api/v1')
        return cm.save('admin', body)

    def test_image_capabilities_expose_background_and_repair_refs(self):
        ch = self.image_channel()
        cap = params.capabilities(cm.version(ch['id']))
        self.assertEqual(cap['profile'], 'lechuang_image')
        self.assertEqual(set(cap['fields']), {'size', 'quality', 'background'})
        self.assertIn('transparent', cap['fields']['background'])
        self.assertIn('1024x1280', cap['fields']['size'])
        self.assertEqual((cap['reference_min'], cap['reference_max']), (0, 9))

    def test_video_capabilities_refs_follow_model(self):
        cap10 = params.capabilities(cm.version(self.video_channel('Grok Image Video')['id']))
        self.assertEqual((cap10['reference_min'], cap10['reference_max']), (0, 7))
        self.assertEqual(set(cap10['fields']), {'ratio', 'resolution', 'duration'})
        self.assertIn('480p', cap10['fields']['resolution'])
        cap15 = params.capabilities(cm.version(self.video_channel('grok-video-1.5')['id']))
        self.assertEqual(cap15['reference_max'], 1)

    def test_image_request_text_to_image(self):
        ch = self.image_channel()
        cfg = cm.version(ch['id'])
        payload = {'prompt': '画一只猫', 'size': '1024x1280', 'quality': 'high', 'background': 'transparent', 'count': 1}
        path, body, files = runtime.build_generation_request(cfg, payload)
        self.assertEqual(path, '/generations')
        self.assertEqual(body['model'], 'gpt-image-2')
        self.assertEqual(body['input']['mode'], 'text_to_image')
        self.assertEqual(body['input']['resolution'], '1k')
        self.assertEqual(body['input']['aspect_ratio'], '4:5')
        self.assertEqual(body['input']['background'], 'transparent')
        self.assertNotIn('reference_images', body['input'])
        self.assertIsNone(files)

    def test_image_request_image_to_image_passes_refs_as_data_url(self):
        ch = self.image_channel()
        cfg = cm.version(ch['id'])
        payload = {'prompt': '改背景', 'size': '1024x1024', 'quality': 'medium', 'background': 'auto',
                   'reference_images': ['data:image/png;base64,AAAA'], 'count': 1}
        path, body, files = runtime.build_generation_request(cfg, payload)
        self.assertEqual(body['input']['mode'], 'image_to_image')
        self.assertEqual(body['input']['reference_images'], [{'type': 'data_url', 'value': 'data:image/png;base64,AAAA'}])

    def test_image_request_http_ref_keeps_url_type(self):
        ch = self.image_channel()
        cfg = cm.version(ch['id'])
        payload = {'prompt': 'x', 'size': '1024x1024', 'quality': 'auto', 'background': 'auto',
                   'reference_images': ['https://example.com/a.png']}
        _, body, _ = runtime.build_generation_request(cfg, payload)
        self.assertEqual(body['input']['reference_images'][0], {'type': 'url', 'value': 'https://example.com/a.png'})

    def test_video_request_maps_size_by_ratio_and_resolution(self):
        ch = self.video_channel()
        cfg = cm.version(ch['id'])
        payload = {'prompt': '海边日出', 'ratio': '9:16', 'resolution': '480p', 'duration': 8}
        path, body, files = runtime.build_generation_request(cfg, payload)
        self.assertEqual(path, '/generations')
        self.assertEqual(body['model'], 'Grok Image Video')
        self.assertEqual(body['input']['mode'], 'text_to_video')
        self.assertEqual(body['input']['size'], '480x848')
        self.assertEqual(body['input']['resolution'], '480p')
        self.assertEqual(body['input']['duration_seconds'], 8)
        payload.update(ratio='16:9', resolution='1080p')
        _, body, _ = runtime.build_generation_request(cfg, payload)
        self.assertEqual(body['input']['size'], '1920x1080')

    def test_video_request_image_to_video(self):
        ch = self.video_channel()
        cfg = cm.version(ch['id'])
        payload = {'prompt': '动起来', 'ratio': '1:1', 'resolution': '720p', 'duration': 4,
                   'reference_images': ['data:image/png;base64,AAAA']}
        _, body, _ = runtime.build_generation_request(cfg, payload)
        self.assertEqual(body['input']['mode'], 'image_to_video')
        self.assertEqual(body['input']['size'], '720x720')
        self.assertEqual(body['input']['reference_images'], [{'type': 'data_url', 'value': 'data:image/png;base64,AAAA'}])

    def test_validation_rejects_video_refs_and_bad_values(self):
        ch = self.video_channel()
        cfg = cm.version(ch['id'])
        with self.assertRaisesRegex(ValueError, '视频参考'):
            runtime.validate_payload(cfg, {'prompt': 'x', 'reference_videos': ['v'], 'ratio': '9:16',
                                           'resolution': '720p', 'duration': 5})
        with self.assertRaisesRegex(ValueError, '最多 7 张'):
            runtime.validate_payload(cfg, {'prompt': 'x', 'reference_images': ['i'] * 8, 'ratio': '9:16',
                                           'resolution': '720p', 'duration': 5})
        with self.assertRaisesRegex(ValueError, '480p/720p/1080p'):
            runtime.validate_payload(cfg, {'prompt': 'x', 'ratio': '9:16', 'resolution': '2k', 'duration': 5})
        with self.assertRaisesRegex(ValueError, '1～15秒'):
            runtime.validate_payload(cfg, {'prompt': 'x', 'ratio': '9:16', 'resolution': '720p', 'duration': 30})
        with self.assertRaisesRegex(ValueError, '9:16、16:9或1:1'):
            runtime.validate_payload(cfg, {'prompt': 'x', 'ratio': '21:9', 'resolution': '720p', 'duration': 5})
        cfg15 = cm.version(self.video_channel('grok-video-1.5')['id'])
        with self.assertRaisesRegex(ValueError, '最多 1 张'):
            runtime.validate_payload(cfg15, {'prompt': 'x', 'reference_images': ['a', 'b'], 'ratio': '9:16',
                                             'resolution': '720p', 'duration': 5})

    def test_image_validation_rejects_too_many_refs_and_bad_background(self):
        cfg = cm.version(self.image_channel()['id'])
        with self.assertRaisesRegex(ValueError, '最多 9 张'):
            runtime.validate_payload(cfg, {'prompt': 'x', 'reference_images': ['i'] * 10})
        with self.assertRaisesRegex(ValueError, 'auto/opaque/transparent'):
            runtime.validate_payload(cfg, {'prompt': 'x', 'background': 'glow'})

    def test_mapping_whitelist_allows_grok15(self):
        ch = self.video_channel('grok-video-1.5')
        cm.save_mapping('admin', dict(kind='xiaole_video', front='grok15', label='Grok 1.5', channel=ch['id'], enabled=True))
        with self.assertRaisesRegex(ValueError, 'grok15'):
            cm.save_mapping('admin', dict(kind='xiaole_video', front='grok99', label='x', channel=ch['id'], enabled=True))

    def test_lechuang_media_target_keeps_key_on_provider_host_only(self):
        ch = self.image_channel()
        cfg = cm.version(ch['id'], with_secret=True)
        relative, needs_auth = runtime._lechuang_media_target(cfg, '/api/v1/generations/REQ1/content')
        self.assertEqual(relative, 'https://api.lechuang.chat/api/v1/generations/REQ1/content')
        self.assertTrue(needs_auth)
        same_origin, same_auth = runtime._lechuang_media_target(cfg, 'https://api.lechuang.chat/files/a.png')
        self.assertTrue(same_auth)
        cdn, cdn_auth = runtime._lechuang_media_target(cfg, 'https://cdn.example.com/a.png')
        self.assertEqual(cdn, 'https://cdn.example.com/a.png')
        self.assertFalse(cdn_auth)   # 第三方 CDN 绝不能带上供应商密钥

    def test_download_lechuang_attaches_auth_only_for_provider_hosts(self):
        ch = self.image_channel()
        cfg = cm.version(ch['id'], with_secret=True)
        calls = []

        def fake_download(cfg_, url, headers=None):
            calls.append((url, headers))
            return b'x'

        with patch.object(runtime, '_download', side_effect=fake_download):
            runtime._download_lechuang(cfg, '/api/v1/generations/REQ9/content')
            runtime._download_lechuang(cfg, 'https://cdn.example.com/1.png')
        self.assertEqual(calls[0][0], 'https://api.lechuang.chat/api/v1/generations/REQ9/content')
        self.assertTrue(calls[0][1]['Authorization'].startswith('Bearer '))
        self.assertIsNone(calls[1][1])

    def test_generate_image_flow_uses_unified_envelope(self):
        ch = self.image_channel()
        cfg = cm.version(ch['id'], with_secret=True)
        rid = 'a' * 32
        tiny_png = base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==')
        calls = {'posts': 0, 'idem': None}

        def fake_request(cfg_, method, path, body=None, extra_headers=None, files=None):
            if method == 'POST':
                calls['posts'] += 1
                calls['idem'] = (extra_headers or {}).get('Idempotency-Key')
                return {'code': 200, 'message': 'success', 'data': {
                    'request_id': 'REQ1', 'task_id': 'IMG1', 'model': 'gpt-image-2', 'model_type': 'image',
                    'status': 'pending', 'output': {'text': None, 'images': [], 'videos': []},
                    'billing': {'charged_amount': '2.0000'}, 'error': None}}
            return {'code': 200, 'message': 'success', 'data': {
                'request_id': 'REQ1', 'model': 'gpt-image-2', 'model_type': 'image', 'status': 'succeeded',
                'output': {'text': None, 'images': [{'url': 'https://img.example/1.png'}], 'videos': []},
                'billing': {'charged_amount': '2.0000'}, 'error': None}}

        with patch.object(runtime, 'request', side_effect=fake_request), \
             patch.object(runtime, '_download', return_value=tiny_png), \
             patch.object(core, 'OUT_DIR', Path(self.tmp.name)), \
             patch.object(core, 'public_url', side_effect=lambda name, mime: 'https://hq.example/' + name):
            result = runtime.generate(cfg, {'prompt': 'x', 'count': 1}, rid, 'job-1')
        self.assertEqual(result['type'], 'image')
        self.assertEqual(result['request_id'], 'REQ1')
        self.assertEqual(calls['idem'], rid)
        self.assertEqual(calls['posts'], 1)

    def test_generate_video_flow_downloads_relative_content_url(self):
        ch = self.video_channel()
        cfg = cm.version(ch['id'], with_secret=True)
        rid = 'b' * 32
        fake_mp4 = b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 64
        downloaded = {}

        def fake_request(cfg_, method, path, body=None, extra_headers=None, files=None):
            if method == 'POST':
                return {'code': 200, 'message': 'success', 'data': {
                    'request_id': 'REQ2', 'task_id': 'TASK2', 'model': 'Grok Image Video', 'model_type': 'video',
                    'status': 'pending', 'output': {'text': None, 'images': [], 'videos': []},
                    'billing': {'charged_amount': '20.0000'}, 'error': None}}
            return {'code': 200, 'message': 'success', 'data': {
                'request_id': 'REQ2', 'model': 'Grok Image Video', 'model_type': 'video', 'status': 'succeeded',
                'output': {'text': None, 'images': [], 'videos': [{
                    'content_url': '/api/v1/generations/REQ2/content', 'mime_type': 'video/mp4',
                    'duration_seconds': 5, 'resolution': '720p', 'aspect_ratio': '9:16'}]},
                'billing': {'charged_amount': '20.0000'}, 'error': None}}

        def fake_download(cfg_, url, headers=None):
            downloaded['url'] = url
            downloaded['auth'] = (headers or {}).get('Authorization')
            return fake_mp4

        probe = types.SimpleNamespace(
            stdout=json.dumps({'streams': [{'codec_type': 'video', 'width': 720, 'height': 1280, 'nb_read_frames': '1'}]}),
            stderr='')

        with patch.object(runtime, 'request', side_effect=fake_request), \
             patch.object(runtime, '_download', side_effect=fake_download), \
             patch('subprocess.run', return_value=probe), \
             patch.object(core, 'OUT_DIR', Path(self.tmp.name)), \
             patch.object(core, 'public_url', side_effect=lambda name, mime: 'https://hq.example/' + name):
            result = runtime.generate(cfg, {'prompt': 'x', 'ratio': '9:16', 'resolution': '720p', 'duration': 5, 'count': 1}, rid, 'job-2')
        self.assertEqual(result['type'], 'video')
        self.assertEqual(result['request_id'], 'REQ2')
        self.assertEqual(downloaded['url'], 'https://api.lechuang.chat/api/v1/generations/REQ2/content')
        self.assertTrue(downloaded['auth'].startswith('Bearer '))

    def test_generate_propagates_provider_failure(self):
        ch = self.image_channel()
        cfg = cm.version(ch['id'])

        def fake_request(cfg_, method, path, body=None, extra_headers=None, files=None):
            return {'code': 200, 'message': 'success', 'data': {
                'request_id': 'REQ3', 'model': 'gpt-image-2', 'model_type': 'image', 'status': 'failed',
                'output': {'text': None, 'images': [], 'videos': []},
                'error': {'message': '上游额度不足', 'code': 'E_QUOTA'}, 'billing': {}}}

        with patch.object(runtime, 'request', side_effect=fake_request), \
             patch.object(core, 'OUT_DIR', Path(self.tmp.name)):
            with self.assertRaisesRegex(runtime.ProviderError, '上游额度不足'):
                runtime.generate(cfg, {'prompt': 'x', 'count': 1}, 'c' * 32, 'job-3')


if __name__ == '__main__':
    unittest.main()
