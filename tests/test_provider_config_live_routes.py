"""Live-route admission and transports, isolated PostgreSQL, no supplier calls."""
import base64
import json
import os
import sys
import types
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from tests import test_provider_config_loop as loop

pc = loop.pc
CASES = [
    ('image', {'provider': 'banana'}, 'image.banana.nb2'),
    ('image', {'provider': 'openai'}, 'image.openai'),
    ('image', {'provider': 'seedream'}, 'image.seedream'),
    ('tryon', {'line': '1'}, 'video.tryon.classic'),
    ('tryon', {'person_image_data': 'fixture'}, 'video.tryon.fast'),
    ('xiaole_video', {'channel': 'micro', 'upscale': True}, 'video.tryon.fast'),
    ('script_to_video', {'material_plan': [{'source': 'generate'}]}, 'image.openai'),
]


class LiveRoutesTests(unittest.TestCase):
    tearDown = loop.SeedreamLoopTest.tearDown

    def setUp(self):
        loop.SeedreamLoopTest.setUp(self)
        os.environ['CONTENT_OUT'] = str(self.tmp / 'output')
        os.environ[pc.WIRING_ENV] = 'all'
        for name in ('GEMINI_API_KEY', 'OPENAI_API_KEY', 'RUNNINGHUB_API_KEY', 'WAVESPEED_API_KEY'):
            os.environ[name] = 'fixture-A'
        os.environ['HQ_PROVIDER_BASE_HOST_ALLOWLIST'] = 'replacement.invalid'

    def publish(self, target):
        draft = pc.save_draft(target, url='https://replacement.invalid/api', secret='fixture-B', actor='test')
        pc.validate_draft(target, draft['seq'], actor='test', probe=loop.probe_all_ok)
        pc.publish(target, draft['seq'], pc.active_version(target)['seq'], 'publish-'+target+str(draft['seq']), actor='test')
        return draft

    def test_every_admission_pins_and_replays_after_switch_off(self):
        for kind, body, target in CASES:
            with self.subTest(kind=kind, target=target):
                os.environ[pc.WIRING_ENV] = 'all'
                original = pc.prepare_job_payload(kind, {**body, '_provider_config': {'version': 999}}, 'test')
                old = pc.job_credentials(target, original['_provider_config'])
                draft = self.publish(target)
                new = pc.prepare_job_payload(kind, body, 'test')
                self.assertEqual(new['_provider_config']['version'], draft['seq'])
                os.environ[pc.WIRING_ENV] = ''
                self.assertEqual(pc.job_credentials(target, original['_provider_config']), old)
                self.assertEqual(pc.job_credentials(target, new['_provider_config'])['credential'], 'fixture-B')

    def test_unrelated_and_managed_routes_keep_existing_contract(self):
        for kind, body in [('audio', {}), ('xiaole_video', {'channel':'micro'}),
                           ('script_to_video', {'material_plan':[{'source':'upload'}]}),
                           ('image', {'provider':'banana', '_channel_binding':{'id':1}})]:
            result = pc.prepare_job_payload(kind, {**body, '_provider_config': {'version':123}})
            self.assertNotIn('_provider_config', result)

    def test_snapshot_failure_happens_before_charge(self):
        from content_domains import jobs_store, channel_manager, channel_parameters
        charge = MagicMock()
        with patch.object(channel_manager, 'capture', side_effect=lambda k,p: p), \
                patch.object(pc, 'pin', side_effect=RuntimeError('db unavailable')):
            for kind, body, target in CASES:
                with self.assertRaises(jobs_store.PaidJobDeductError):
                    jobs_store.create_paid_job(MagicMock(), charge, MagicMock(), kind, 'test', 1, body, 'test')
        charge.assert_not_called()

    def test_openai_transport_uses_pinned_url_and_key_for_both_paths(self):
        from content_domains import image, egress
        payload = pc.prepare_job_payload('image', {'provider':'openai'})
        original = pc.job_credentials('image.openai', payload['_provider_config'])
        self.publish('image.openai')
        for stream in (False, True):
            transport = 'post_image_json' if stream else 'post_json'
            with patch.object(egress, transport, return_value={'data':[]}) as send:
                image._dispatch_gpt('openai', '/v1/images/generations', b'{}', 'application/json', '', '', False,
                                    streaming=stream, config_ref=payload['_provider_config'])
            args = send.call_args.args
            self.assertEqual(args[:2], (original['url'], original['url']))
            self.assertEqual(args[4]['Authorization'], 'Bearer '+original['credential'])

    def test_both_banana_workers_use_pinned_credentials(self):
        from content_domains import banana_provider, egress
        import imggen_api
        payload = pc.prepare_job_payload('image', {'provider':'banana', 'prompt':'fixture'})
        original = pc.job_credentials('image.banana.nb2', payload['_provider_config'])
        self.publish('image.banana.nb2')
        response = {'candidates':[{'content':{'parts':[{'inlineData':{'data':base64.b64encode(b'image').decode()}}]}}]}
        with patch.object(egress, 'post_json', return_value=response) as send, \
                patch.object(banana_provider, '_normalize_ratio', return_value=(b'image',None)):
            banana_provider.generate(payload, self.tmp, lambda *a:'https://output.invalid/image')
        self.assertEqual(send.call_args.args[:2], (original['url'], original['url']))
        self.assertEqual(send.call_args.args[4]['x-goog-api-key'], original['credential'])
        with patch.object(egress, 'post_json', return_value=response) as send, \
                patch.object(imggen_api, 'OUT_DIR', self.tmp), \
                patch.object(imggen_api, '_normalize_image_ratio', return_value=(b'image',None)):
            imggen_api.gen_banana(payload)
        self.assertEqual(send.call_args.args[:2], (original['url'], original['url']))
        self.assertEqual(send.call_args.args[4]['x-goog-api-key'], original['credential'])

    def test_wavespeed_resume_only_gets_original_host_with_original_key(self):
        from content_domains import wavespeed
        payload = pc.prepare_job_payload('xiaole_video', {'channel':'micro', 'upscale':True})
        original = pc.job_credentials('video.tryon.fast', payload['_provider_config'])
        self.publish('video.tryon.fast')
        with patch.object(wavespeed, '_ws_req', return_value={'code':200,'data':{'status':'completed','outputs':['https://output.invalid/a.mp4']}}) as send, \
                patch.object(wavespeed, '_public_http_url_state', return_value='ok'):
            wavespeed.run_seedvr2(prediction_id='old-id', config_ref=payload['_provider_config'])
        send.assert_called_once()
        self.assertEqual(send.call_args.args[:2], ('GET',original['url']+'/predictions/old-id/result'))
        self.assertEqual(send.call_args.kwargs['credential'], original['credential'])

    def test_runninghub_client_receives_frozen_pair(self):
        from content_domains import video
        payload = pc.prepare_job_payload('tryon', {'line':'1'})
        original = pc.job_credentials('video.tryon.classic',payload['_provider_config'])
        self.publish('video.tryon.classic')
        sdk = types.ModuleType('runninghub_sdk')
        sdk.RunningHubClient = MagicMock(side_effect=RuntimeError('stop before upload'))
        with patch.dict(sys.modules, {'runninghub_sdk':sdk}):
            with self.assertRaisesRegex(RuntimeError,'stop before upload'):
                video.generate_tryon_video('fixture.mp4',None,None,6,config_ref=payload['_provider_config'])
        sdk.RunningHubClient.assert_called_once_with(original['credential'],base_url=original['url'],timeout=120)

    def test_material_retry_preserves_parent_version(self):
        import urllib.error
        from content_domains import image, script_to_video
        payload = pc.prepare_job_payload('script_to_video',{'material_plan':[{'source':'generate','scene_index':0,'prompt':'fixture'}]})
        self.publish('image.openai')
        with patch.object(image,'gen_image',side_effect=[urllib.error.HTTPError('https://fixture.invalid',520,'',{},None),{'file':'fixture.png'}]) as send, \
                patch.object(script_to_video,'_safe_existing_image',return_value=True), \
                patch.object(script_to_video.time,'sleep'):
            script_to_video._material_images(payload['material_plan'],payload['_provider_config'])
        self.assertEqual(send.call_count,2)
        for call in send.call_args_list:
            self.assertEqual(call.args[0]['_provider_config'],payload['_provider_config'])

    def test_probe_contracts_and_http200_error_body(self):
        responses = {'image.openai':('/v1/models', {'data':[]}),
                     'image.banana.nb2':('/v1beta/models', {'models':[]}),
                     'video.tryon.fast':('/balance', {'code':200,'data':{'balance':0}}),
                     'video.tryon.classic':('/uc/openapi/accountStatus', {'code':0,'data':{'remainCoins':'0'}})}
        for target,(path,body) in responses.items():
            for valid in (True, False):
                response = MagicMock()
                response.__enter__.return_value.status = 200
                response.__enter__.return_value.read.return_value = json.dumps(body if valid else {'error':'invalid fixture key'}).encode()
                with patch('urllib.request.build_opener') as opener:
                    opener.return_value.open.return_value = response
                    result = pc._provider_probe(target,'https://fixture.invalid','fixture-secret')
                req = opener.return_value.open.call_args.args[0]
                self.assertEqual(req.full_url,'https://fixture.invalid'+path)
                self.assertEqual(result['auth']['ok'],valid)
                self.assertNotIn('fixture-secret',json.dumps(result))

    def test_url_version_suffix_normalized(self):
        self.assertEqual(pc.validate_url('image.openai','https://api.openai.com/v1'), 'https://api.openai.com')
        self.assertEqual(pc.validate_url('image.banana.nb2','https://generativelanguage.googleapis.com/v1beta'), 'https://generativelanguage.googleapis.com')

    def test_environment_fallback_is_frozen_not_silently_switched(self):
        from content_domains import image, egress
        for provider,prefix,target in [('openai','OPENAI','image.openai'),('banana','GEMINI','image.banana.nb2')]:
            with self.subTest(provider=provider):
                os.environ[prefix+'_BASE'] = 'https://old-relay.invalid'
                os.environ.pop(prefix+'_OFFICIAL_BASE',None)
                old = pc.prepare_job_payload('image', {'provider':provider})['_provider_config']
                os.environ[prefix+'_BASE'] = 'https://new-relay.invalid'
                config = pc.job_credentials(target,old)
                self.assertEqual(config['fallback_url'],'https://old-relay.invalid')
                self.publish(target)
                new = pc.prepare_job_payload('image',{'provider':provider})['_provider_config']
                self.assertNotIn('fallback_url',pc.job_credentials(target,new))
                if provider == 'openai':
                    with patch.object(egress,'post_json') as send:
                        image._dispatch_gpt('openai','/v1/images/edits',b'{}','application/json','','',False,config_ref=old)
                    self.assertEqual(send.call_args.args[:2],('https://api.openai.com','https://old-relay.invalid'))


if __name__ == '__main__':
    unittest.main()
