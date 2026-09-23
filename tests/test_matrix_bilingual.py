import importlib
from pathlib import Path
import sys
import json
import subprocess
import tempfile
import hashlib
import time
import shutil
import wave
from contextlib import ExitStack
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
module=importlib.import_module('content_domains.matrix_bilingual')


class BilingualAlignmentTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg required')
    def test_narration_mux_does_not_loop_or_reencode_finished_video(self):
        video=importlib.import_module('content_domains.matrix_template_video')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'video').mkdir()
            target=root/'video/clip.mp4';audio=root/'voice.wav'
            subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=blue:s=1080x1920:r=30:d=1',
                '-c:v','libx264','-preset','veryfast','-bf','2','-pix_fmt','yuv420p',str(target)],check=True,capture_output=True,timeout=30)
            with wave.open(str(audio),'wb') as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(b'\0'*(6400*2))
            def packets():
                return subprocess.run(['ffmpeg','-v','error','-i',str(target),'-map','0:v:0','-c:v','copy','-f','hash','-hash','sha256','pipe:1'],check=True,capture_output=True,timeout=20).stdout
            before=packets()
            with mock.patch.object(video,'OUT_DIR',root):
                duration,_=video._mux_voiceover('video/clip.mp4',{'path':audio,'duration':.4},time.time()+60,narration_duration=1.)
            self.assertEqual(before,packets())
            self.assertAlmostEqual(1.,duration,places=2)

    def test_generation_freezes_plan_before_submit_and_reuses_it_on_retry(self):
        video=importlib.import_module('content_domains.matrix_template_video')
        with tempfile.TemporaryDirectory() as temp:
            audio=Path(temp)/'voice.mp3';audio.write_bytes(b'test-audio')
            plan=module.align_cues('我在广州',self.cues(),self.words(),4.,hashlib.sha256(audio.read_bytes()).hexdigest())
            plan['visual_count']=3
            materials=[{'sha256':str(i)*64,'media_type':'video','clip_start_seconds':.2} for i in range(3)]
            public={'template_id':module.TEMPLATE_ID,'top_text':'广州圈子','bottom_text':'共同成长','duration':8,'bgm':False,
                    'user_materials':materials,'_matrix_material_durations':[5.,5.,5.],
                    'semantic_layout':{'version':1},'voiceover':{'voice':'fixture','text':'我在广州','voice_scope':'public'}}
            bodies=[]
            for saved in (None,plan):
                events=[]
                lifecycle={'created_at':int(time.time()),'payload':dict(public,_matrix_runtime={} if saved is None else {'phase':'submission_unknown','bilingual_plan':saved,'bilingual_materials':materials})}
                def request(method,route,body=None,**kwargs):
                    if method=='POST':
                        events.append('submit');bodies.append(body)
                        return {'job_id':'d'*32}
                    return {'status':'completed','result':{'duration':4.6,'file_url':'/v1/files/result.mp4'}}
                def persist(_job,**kwargs):
                    if 'bilingual_plan' in kwargs:events.append('freeze')
                    return True
                with ExitStack() as stack:
                    for name,value in {'_runtime':lifecycle,'validate_payload':dict(public),'_prepare_voiceover_audio':{'path':audio,'duration':4.},
                                       '_download':('video/result.mp4',2048),'_mux_voiceover':(4.6,2048),'public_url':'/out.mp4'}.items():
                        stack.enter_context(mock.patch.object(video,name,return_value=value))
                    stack.enter_context(mock.patch.object(video,'_persist_runtime',side_effect=persist))
                    stack.enter_context(mock.patch.object(video,'_request',side_effect=request))
                    prepare=stack.enter_context(mock.patch.object(module,'prepare',return_value=plan))
                    result=video._generate(dict(public,_job_id='101',_username='alice'))
                    self.assertEqual(4.6,result['duration'])
                    if saved is None:
                        self.assertLess(events.index('freeze'),events.index('submit'));prepare.assert_called_once()
                    else:prepare.assert_not_called()
            self.assertEqual(bodies[0],bodies[1])
            self.assertNotIn('voiceover',bodies[0])
            self.assertEqual(4.6,bodies[0]['duration'])
            self.assertEqual(plan,bodies[0]['narration_plan'])
            self.assertNotIn(str(audio),json.dumps(bodies[0]))

    def test_browser_selects_original_bilingual_mode_and_restores_other_templates(self):
        result=subprocess.run(['node',str(Path(__file__).with_name('matrix_template_page_runtime.js')),'motionV3'],capture_output=True,text=True,encoding='utf-8',check=True,timeout=20)
        value=json.loads(result.stdout)
        self.assertEqual('副标题',value['selected']['bottom'])
        self.assertTrue(value['selected']['voiceLocked'])
        self.assertTrue(value['selected']['bgmLocked'])
        self.assertEqual(module.TEMPLATE_ID,value['body']['template_id'])
        self.assertFalse(value['body']['bgm'])
        self.assertEqual('我在广州',value['body']['voiceover']['text'])
        self.assertEqual('底部行动文案',value['restoredLabel'])
        self.assertTrue(value['bgmVoiceOff'])
        self.assertTrue(value['toggleAbsent'])
        self.assertTrue(value['bgmBody']['bgm'])
        self.assertNotIn('voiceover', value['bgmBody'])
    def words(self):
        return [{'text':'我在','start_ms':500,'end_ms':1000,'timing_source':'provider_word'},
                {'text':'广州','start_ms':2500,'end_ms':3500,'timing_source':'provider_word'}]

    def cues(self):
        return [{'text':'我在广州','en':'Here in Guangzhou','yellow':[2,3]}]

    def test_uses_real_word_spans_and_preserves_pauses(self):
        p=module.align_cues('我在广州',self.cues(),self.words(),4.,'a'*64)
        self.assertEqual([.5,.75,2.5,3.],p['cues'][0]['times'])
        self.assertEqual([2,3],p['cues'][0]['yellow'])
        self.assertEqual(4.6,p['duration'])
        self.assertEqual(4.6,p['cues'][0]['end'])

    def test_segment_estimates_are_not_allowed(self):
        words=self.words();words[0]['timing_source']='segment_interpolated'
        with self.assertRaisesRegex(ValueError,'不允许估算'):
            module.align_cues('我在广州',self.cues(),words,4.,'a'*64)

    def test_rewritten_or_missing_source_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'不一致'):
            module.align_cues('我在深圳',self.cues(),self.words(),4.,'a'*64)
        with self.assertRaisesRegex(ValueError,'漏字或错字'):
            module.align_cues('我在广州',self.cues(),self.words()[:1],4.,'a'*64)
        with self.assertRaisesRegex(ValueError,'不一致'):
            module.align_cues('1-2天',[{'text':'12天','en':'days'}],[],4.,'a'*64)

    def test_invalid_words_or_translation_fail(self):
        for cues,words in [([{'text':'我在广州','en':''}],self.words()),(self.cues(),[dict(self.words()[0],end_ms=-1)])]:
            with self.assertRaises(ValueError):module.align_cues('我在广州',cues,words,4.,'a'*64)

    def test_original_mode_requires_narration_and_forbids_bgm_before_submission(self):
        video=importlib.import_module('content_domains.matrix_template_video')
        available=mock.patch.object(video,'require_available');available.start();self.addCleanup(available.stop)
        template={'id':module.TEMPLATE_ID,'name':'双语','font_selectable':False,'requires_voiceover':True,'bgm_mode':'none','duration_mode':'narration'}
        body={'template_id':module.TEMPLATE_ID,'top_text':'广州圈子','bottom_text':'共同成长'}
        with mock.patch.object(video,'public_templates',return_value=[template]),mock.patch.object(video,'_resolve_template_tuning',return_value=(None,None,None)),mock.patch.object(video,'_normalize_voiceover',return_value=None):
            with self.assertRaisesRegex(ValueError,'启用口播'):
                video.validate_payload(body,'alice')
        with mock.patch.object(video,'public_templates',return_value=[template]),mock.patch.object(video,'_resolve_template_tuning',return_value=(None,None,None)),mock.patch.object(video,'_normalize_voiceover',return_value={'text':'我在广州'}),mock.patch.object(module,'ensure_ready'):
            with self.assertRaisesRegex(ValueError,'不使用背景音乐'):
                video.validate_payload(dict(body,bgm=True),'alice')


if __name__=='__main__':unittest.main()
