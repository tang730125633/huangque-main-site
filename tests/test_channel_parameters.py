import copy
import json
import sqlite3
import base64
import io
import sys
import types
from pathlib import Path
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch
import tests.test_channel_manager as base
from server.content_domains import channel_manager as cm, channel_parameters as params, channel_runtime as runtime, jobs_store


class ParameterTests(unittest.TestCase):
    setUp=base.ChannelTests.setUp

    def spec(self,profile=None):
        cfg=cm.version(self.ch['id']);cap=params.capabilities(cfg,profile)
        values={k:v[0] for k,v in cap['fields'].items()}
        return dict(profile=cap['profile'],fields=[dict(key=k,label=params.LABELS[k],visible=True) for k in cap['fields']],
            combinations=[dict(id='first',values=values,points=20)],default='first',reference_min=cap['reference_min'],reference_max=cap['reference_max'])

    def mapping(self,kind='image',front='public-model'):
        cm.save_mapping('admin',dict(kind=kind,front=front,label='黄雀模型',channel=self.ch['id'],enabled=True))

    def draft(self,spec=None):
        state=params.admin_state(self.ch['id'])
        return params.change('admin',dict(id=self.ch['id'],version=state['version'],draft_revision=(state['draft'] or {}).get('revision',0),action='draft',parameters=spec or self.spec()))

    def publish(self,spec=None):
        state=self.draft(spec)
        return params.change('admin',dict(id=self.ch['id'],version=state['version'],draft_revision=state['draft']['revision'],action='publish',confirmed=True))

    def payload(self,kind='image'):
        item=params.public_catalog()['items'][0]
        return dict(prompt='test',**({'model':item['front']} if kind=='image' else {'channel':item['front']}),
                    parameter_selection={'revision':item['revision'],'combination':item['default']})

    def test_draft_is_private_and_publish_has_only_public_fields(self):
        self.mapping();self.draft()
        self.assertEqual(params.public_catalog()['items'],[])
        self.publish();public=params.public_catalog();raw=json.dumps(public)
        for secret in ['private-secret','127.0.0.1','base_url','test-model','actor','draft']:
            self.assertNotIn(secret,raw)
        self.assertEqual(public['items'][0]['label'],'黄雀模型')
        self.assertEqual(public['items'][0]['combinations'][0]['points'],20)

    def test_capture_transforms_size_quality_and_quotes_same_points(self):
        self.mapping();self.publish(self.spec('gpt_image'))
        payload=self.payload();payload['size']='9000x9000';payload['quality']='fake'
        captured=cm.capture('image',payload)
        self.assertEqual(captured['size'],'1024x1024');self.assertEqual(captured['quality'],'low')
        self.assertEqual(params.quote('image',payload),20)
        cfg=cm.version(self.ch['id'])
        request=params.image_request(cfg,captured)
        self.assertEqual(request['size'],'1024x1024');self.assertNotIn('response_format',request)

    def test_stale_selection_can_be_quoted_for_replay_but_not_new_admission(self):
        self.mapping();self.publish();old=self.payload()
        spec=self.spec();spec['combinations'][0]['points']=35;self.publish(spec)
        self.assertEqual(params.quote('image',old,allow_historical=True),20)
        with self.assertRaisesRegex(ValueError,'已更新'):cm.capture('image',old)
        self.assertEqual(params.quote('image',self.payload()),35)

    def test_no_client_snapshot_or_cost_can_bypass_contract(self):
        self.mapping();self.publish();payload={'model':'public-model','prompt':'test','_channel_binding':self.ch,'points':1}
        with self.assertRaises(ValueError):cm.capture('image',payload)
        payload=self.payload();calls=[]
        with self.assertRaises(jobs_store.PaidJobDeductError):
            jobs_store.create_paid_jobs(None,lambda *a:calls.append(a),None,'image','user',[(1,payload)],'worker')
        self.assertEqual(calls,[])

    def test_disallowed_values_transparency_and_hidden_variants(self):
        for modify in [lambda s:s['combinations'][0]['values'].update(size='4096x4096'),
                       lambda s:s['combinations'][0]['values'].update(background='transparent',output_format='jpeg'),
                       lambda s:s['combinations'][0].update(points=0),
                       lambda s:s.update(default='absent')]:
            spec=self.spec('gpt_image');modify(spec)
            with self.assertRaises(ValueError):self.draft(spec)
        spec=self.spec('gpt_image');spec['fields'][0]['visible']=False
        extra=copy.deepcopy(spec['combinations'][0]);extra['id']='second';extra['values']['size']='1536x1024';spec['combinations'].append(extra)
        with self.assertRaisesRegex(ValueError,'隐藏'):self.draft(spec)

    def test_reference_and_count_constraints(self):
        self.mapping();self.publish()
        for extra in [{'reference_images':['x']},{'count':2}]:
            with self.assertRaises(ValueError):cm.capture('image',dict(self.payload(),**extra))

    def test_duplicate_combinations_rejected(self):
        spec=self.spec();extra=copy.deepcopy(spec['combinations'][0]);extra['id']='second';spec['combinations'].append(extra)
        with self.assertRaisesRegex(ValueError,'重复'):self.draft(spec)

    def test_gpt_image_2_extended_sizes_are_profile_specific(self):
        cfg=dict(cm.version(self.ch['id']),model='gpt-image-2')
        cap=params.capabilities(cfg)
        self.assertEqual(cap['profile'],'gpt_image_2');self.assertIn('3840x2160',cap['fields']['size'])
        self.assertNotIn('3840x3840',cap['fields']['size'])
        with self.assertRaises(ValueError):params.capabilities(dict(cfg,model='gpt-image-1'),'gpt_image_2')

    def test_request_preview_uses_runtime_builder_without_calling_provider(self):
        spec=self.spec('gpt_image')
        with patch.object(runtime,'request') as request:
            result=params.preview('admin',dict(id=self.ch['id'],version=1,parameters=spec))
        request.assert_not_called()
        self.assertEqual(result['path'],'/images/generations');self.assertEqual(result['points'],20)
        self.assertEqual(result['body']['size'],'1024x1024');self.assertNotIn('response_format',result['body'])

    def test_concurrent_drafts_only_one_wins(self):
        body=dict(id=self.ch['id'],version=1,draft_revision=0,action='draft',parameters=self.spec())
        def save(actor):
            try:return params.change(actor,body)
            except ValueError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(save,['a','b']))
        self.assertEqual(sum(r is not None for r in results),1)

    def test_connection_edit_invalidates_pending_draft_and_preserves_publication(self):
        self.publish();state=self.draft()
        current=cm.overview()['items'][0]
        cm.save('admin',dict(current,name='new name'))
        with self.assertRaisesRegex(ValueError,'版本已变化'):
            params.change('admin',dict(id=self.ch['id'],version=state['version'],draft_revision=state['draft']['revision'],action='publish',confirmed=True))
        self.assertEqual(cm.version(self.ch['id'])['parameters']['default'],'first')

    def test_parameter_rollback_creates_new_version_and_keeps_old_task_config(self):
        self.mapping();first=self.publish();old=cm.capture('image',self.payload())
        spec=self.spec();spec['combinations'][0]['points']=40;state=self.publish(spec)
        result=params.change('admin',dict(id=self.ch['id'],version=state['version'],draft_revision=state['draft']['revision'],action='rollback',target_version=first['version'],confirmed=True))
        self.assertGreater(result['version'],state['version'])
        cfg=cm.version(old['_channel_binding']['id'],old['_channel_binding']['version'])
        self.assertEqual(params.apply(cfg,old)[1],20)

    def test_disabled_channel_disappears_from_catalog(self):
        self.mapping();self.publish();current=cm.overview()['items'][0]
        cm.save('a',dict(current,enabled=False))
        self.assertEqual(params.public_catalog()['items'],[])

    def test_unmapping_does_not_silently_fall_back_with_parameter_selection(self):
        self.mapping();self.publish();payload=self.payload()
        with closing(cm.db()) as c:c.execute('DELETE FROM mappings');c.commit()
        with self.assertRaisesRegex(ValueError,'映射已变化'):cm.capture('image',payload)

    def test_minimax_and_xai_limits_use_existing_adapters(self):
        for adapter,model,front in [('minimax_h3','MiniMax-H3','minimax'),('xai_video','grok-imagine-video','grok')]:
            self.ch=cm.save('a',dict(self.body,adapter=adapter,model=model))
            self.mapping('xiaole_video',front);self.publish()
            cfg=cm.version(self.ch['id']);spec=cfg['parameters'];self.assertEqual(spec['reference_max'],5 if front=='minimax' else 1)
            payload=dict(channel=front,prompt='test',parameter_selection={'revision':params.token(cfg),'combination':'first'})
            captured=cm.capture('xiaole_video',payload);runtime.validate_payload(cfg,captured)
            self.assertEqual(captured['resolution'],'2K' if front=='minimax' else '720p')

    def test_video_preparation_can_replay_old_revision_but_admission_rejects_it(self):
        self.ch=cm.save('a',dict(self.body,adapter='xai_video',model='grok-imagine-video'))
        self.mapping('xiaole_video','grok');self.publish();old=self.payload('xiaole_video')
        self.publish();prepared=cm.capture('xiaole_video',old,preparation=True)
        self.assertIn('_channel_binding',prepared)
        with self.assertRaises(ValueError):cm.capture('xiaole_video',prepared)

    def test_exact_price_is_deducted_and_persisted_with_parameter_snapshot(self):
        self.mapping();self.publish();charges=[]
        def jdb():return sqlite3.connect(self.tmp.name+'/paid.db')
        with closing(jdb()) as c:
            c.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY,kind TEXT,username TEXT,cost INTEGER,payload TEXT,created_at INTEGER,updated_at INTEGER,owner TEXT)');c.commit()
        ids,_=jobs_store.create_paid_jobs(jdb,lambda u,c,r:charges.append(c) or 80,lambda *a:None,'image','user',[(20,self.payload())],'worker')
        with closing(jdb()) as c:row=c.execute('SELECT cost,payload FROM jobs WHERE id=?',(ids[0],)).fetchone()
        self.assertEqual(charges,[20]);self.assertEqual(row[0],20)
        self.assertEqual(json.loads(row[1])['size'],'1024x1024');self.assertIn('_channel_binding',json.loads(row[1]))

    def test_output_format_is_honored_and_wrong_size_is_not_success(self):
        from PIL import Image
        self.mapping();spec=self.spec('gpt_image');spec['combinations'][0]['values']['output_format']='jpeg';self.publish(spec)
        cfg=cm.version(self.ch['id'],with_secret=True);payload=cm.capture('image',self.payload())
        core=types.ModuleType('server.content_domains.core');core.OUT_DIR=Path(self.tmp.name);core.public_url=lambda f,t:'local/'+f
        def output(size):
            data=io.BytesIO();Image.new('RGB',size,'white').save(data,'PNG');return {'data':[{'b64_json':base64.b64encode(data.getvalue()).decode()}]}
        with patch.dict(sys.modules,{'server.content_domains.core':core}),patch('server.content_domains.core',core,create=True),patch.object(runtime,'request',return_value=output((1024,1024))):
            rid=cm.reserve(cfg['id'],'task','job',cfg);result=runtime.generate(cfg,payload,rid,'job')
            with Image.open(Path(self.tmp.name)/result['file']) as image:self.assertEqual(image.format,'JPEG')
        with patch.dict(sys.modules,{'server.content_domains.core':core}),patch('server.content_domains.core',core,create=True),patch.object(runtime,'request',return_value=output((128,128))):
            with self.assertRaisesRegex(ValueError,'尺寸不一致'):runtime.generate(cfg,payload,rid,'job')


if __name__=='__main__':unittest.main()
