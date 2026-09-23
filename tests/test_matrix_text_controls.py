from contextlib import ExitStack
import importlib
import json
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
sys.path.insert(0,str(ROOT/'tools/hq-cli/src'))
styles = importlib.import_module('content_domains.matrix_text_controls')
video = importlib.import_module('content_domains.matrix_template_video')


def controls():
    return {'contract_version':1,'text_revision':'a'*64,'text_tunable':True,'preview_supported':False,
            'layers':{'top1':{'defaults':{'font_family':'Noto Sans SC','font_size_px':80}}},
            'fields':dict(styles.PROPERTIES,font_family={'type':'string','enum':['Noto Sans SC']})}


class TextControlBridgeTests(unittest.TestCase):
    def test_semantic_planner_receives_requested_sizes_without_mutating_defaults(self):
        base={'layers':{'top1':{'font_size_px':80,'max_width_px':900}}}
        definition=controls();definition['layers']['top1']['semantic_layers']=['top1']
        effective=styles.semantic_contract(base,{'top1':{'font_size_px':120,'offset_x_px':20}},definition)
        self.assertEqual({'font_size_px':120,'max_width_px':860},effective['layers']['top1'])
        self.assertEqual(80,base['layers']['top1']['font_size_px'])

    def body(self):
        return {'template_id':'ref-01-fixture','top_text':'测试标题','bottom_text':'测试结尾','bgm':True,
                'text_revision':'a'*64,'text_overrides':{'top1':{'font_size_px':90,'color':'#abcdef'}}}

    def test_controls_are_renderer_owned_and_inputs_are_strict(self):
        schema=controls();self.assertEqual(schema,styles.parse_controls(schema))
        clean=styles.normalize_request(self.body(),schema)
        self.assertEqual('#ABCDEF',clean['text_overrides']['top1']['color'])
        for updates in ({'text_revision':'b'*64},{'preview_id':'p'},{'overrides':{'title_scale':1}},
                        {'text_overrides':{'missing':{'color':'#ffffff'}}},
                        {'text_overrides':{'top1':{'font_family':'unknown'}}},
                        {'text_overrides':{'top1':{'font_size_px':True}}},
                        {'text_overrides':{'top1':{'offset_x_px':float('nan')}}},
                        {'text_overrides':{'top1':{'color':'url(file:///secret)'}}}):
            with self.subTest(updates=updates),self.assertRaises(ValueError):
                styles.normalize_request(dict(self.body(),**updates),schema)
        with self.assertRaises(ValueError):styles.normalize_request(self.body(),None)

    def test_controls_endpoint_exposes_text_styles_without_enabling_legacy_overrides(self):
        with mock.patch.object(video,'_refresh_catalog'),mock.patch.dict(video._CACHE,{'templates':[{'id':'ref-01-fixture','name':'fixture','tunable':False}], 'text_controls':{'ref-01-fixture':controls()}}):
            value=video.public_template_controls('ref-01-fixture')
        self.assertFalse(value['tunable']);self.assertTrue(value['text_tunable'])
        self.assertEqual(controls(),value['text_controls'])

    def test_content_preflight_preserves_overrides_and_rejects_dropped_echo(self):
        template={'id':'ref-01-fixture','font_selectable':False,'engine':'ffmpeg'}
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(video,'require_available'))
            stack.enter_context(mock.patch.object(video,'public_templates',return_value=[template]))
            stack.enter_context(mock.patch.dict(video._CACHE,{'text_controls':{'ref-01-fixture':controls()}}))
            call=stack.enter_context(mock.patch.object(video,'_request',side_effect=lambda method,path,payload,**kw:{'payload':dict(payload,duration=8)}))
            value=video.validate_payload(self.body(),'alice')
            self.assertEqual('a'*64,value['text_revision'])
            self.assertEqual('#ABCDEF',value['text_overrides']['top1']['color'])
            self.assertEqual(value['text_overrides'],call.call_args.args[2]['text_overrides'])
            call.side_effect=lambda method,path,payload,**kw:{'payload':{k:v for k,v in dict(payload,duration=8).items() if k not in {'text_revision','text_overrides'}}}
            with self.assertRaises(RuntimeError):video.validate_payload(self.body(),'alice')

    def test_cli_api_and_local_cli_keep_single_and_batch_text_parameters(self):
        api=importlib.import_module('hq_cli_api')
        cli=importlib.import_module('hq_cli.cli')
        body=self.body()
        self.assertEqual('#ABCDEF',api._matrix_template_payload(body)['text_overrides']['top1']['color'])
        batch_body=dict({k:v for k,v in body.items() if k!='bgm'},count=2)
        batch=api._matrix_template_batch_payload(batch_body)
        self.assertEqual('#ABCDEF',batch['item']['text_overrides']['top1']['color'])
        for action,payload in (('matrix-template-generate',body),('matrix-template-batch-generate',batch_body)):
            cli._validate(cli.CAPABILITIES[action],payload)
            with self.assertRaises(cli.CliError):cli._validate(cli.CAPABILITIES[action],dict(payload,text_revision='bad'))
            with self.assertRaises(cli.CliError):cli._validate(cli.CAPABILITIES[action],dict(payload,text_overrides={'top1':{'font_size_px':False}}))

    def test_uncertain_submit_replays_frozen_styles_and_verifies_result(self):
        public=self.body();public['text_overrides']['top1']['color']='#ABCDEF'
        public['duration']=8;public['semantic_layout']={'version':1}
        lifecycle={'created_at':int(time.time()),'trusted_execution':True,'payload':dict(public,_matrix_runtime={'phase':'submission_unknown'})}
        requests=[]
        def request(method,path,payload=None,**kw):
            requests.append((method,path,payload))
            if method=='POST':return {'job_id':'d'*32}
            return {'status':'completed','result':{'duration':8,'file_url':'/out.mp4','text_revision':'a'*64,'text_overrides':public['text_overrides']}}
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(video,'_runtime',return_value=lifecycle))
            validate=stack.enter_context(mock.patch.object(video,'validate_payload',side_effect=AssertionError('must not regenerate after uncertain submit')))
            stack.enter_context(mock.patch.object(video,'_prepare_voiceover_audio',return_value=None))
            stack.enter_context(mock.patch.object(video,'_persist_runtime',return_value=True))
            call=stack.enter_context(mock.patch.object(video,'_request',side_effect=request))
            download=stack.enter_context(mock.patch.object(video,'_download',return_value=('video/out.mp4',1234)))
            stack.enter_context(mock.patch.object(video,'public_url',return_value='/out.mp4'))
            result=video._generate(dict(public,_job_id='42',_username='alice'))
            validate.assert_not_called()
            self.assertEqual(public['text_overrides'],requests[0][2]['text_overrides'])
            self.assertEqual(public['text_overrides'],result['text_overrides'])
            lifecycle['payload']['_matrix_runtime']={'provider_job_id':'d'*32}
            call.side_effect=lambda *a,**kw:{'status':'completed','result':{'duration':8,'file_url':'/out.mp4'}}
            download.reset_mock()
            with self.assertRaises(video.MatrixTemplateProviderFailed):video._generate(dict(public,_job_id='42',_username='alice'))
            download.assert_not_called()


if __name__=='__main__':unittest.main()
