import hashlib
import importlib
import json
from pathlib import Path
import random
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
media = importlib.import_module('content_domains.matrix_template_account_media')


class AccountMediaTests(unittest.TestCase):
    def test_only_current_account_live_distinct_videos_are_candidates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for i, (owner, expires, sha) in enumerate([
                    ('alice', time.time()+100, 'a'*64),
                    ('bob', time.time()+100, 'b'*64),
                    ('alice', time.time()-100, 'c'*64),
                    ('alice', time.time()+100, 'a'*64)]):
                identifier = 'vid_' + f'{i:032x}'
                (root / (identifier+'.json')).write_text(json.dumps({
                    'version':1, 'owner_hash':hashlib.sha256(owner.encode()).hexdigest(),
                    'mime':'video/mp4', 'duration':12., 'expires_at':expires, 'sha256':sha}), encoding='utf-8')
                (root / (identifier+'.mp4')).write_bytes(b'fixture')
            with mock.patch.object(media.cli_uploads, 'UPLOAD_ROOT', root):
                found = media.available('alice')
                self.assertEqual(1, len(found))
                self.assertNotIn(found[0]['upload_id'], ['vid_'+f'{i:032x}' for i in (1,2)])
                with self.assertRaises(ValueError): media.initial(media.INSET, 'alice')
                with self.assertRaises(ValueError): media.available('')

    def test_windows_include_later_source_sections_and_never_repeat(self):
        records = [{'upload_id':str(i), 'media_type':'video', 'duration':length}
                   for i,length in enumerate((2.8,3.,6.,7.))]
        chosen = media.choose(records, media.WINDOWS[media.OPENING], rng=random.Random(3))
        self.assertEqual(4, len({item['upload_id'] for item in chosen}))
        self.assertTrue(any(item['clip_start_seconds'] > 0 for item in chosen))
        lengths = {r['upload_id']:r['duration'] for r in records}
        for item, window in zip(chosen, media.WINDOWS[media.OPENING]):
            self.assertGreaterEqual(lengths[item['upload_id']], item['clip_start_seconds']+window+.1)

    def test_bilingual_duration_chooses_feasible_count_and_explicit_stays_ordered(self):
        materials = [{'sha256':str(i)*64,'media_type':'video','clip_start_seconds':.2} for i in range(5)]
        chosen = media.bilingual(materials,[8.]*5,12.6)
        self.assertEqual(5,len(chosen))
        explicit = media.bilingual(materials,[8.]*5,12.6,explicit=True)
        self.assertEqual(materials,explicit)
        self.assertEqual(3,len(media.bilingual(materials[:3],[8.]*3,12.6)))
        with self.assertRaises(ValueError): media.bilingual(materials,[2.2]*5,12.6)
        with self.assertRaises(ValueError): media.bilingual(materials,None,12.6)
        with self.assertRaises(ValueError): media.bilingual([materials[0]]*3,[8.]*3,8.,explicit=True)

    def test_shorter_owned_sources_can_use_more_slots(self):
        materials = [{'sha256':f'{i:064x}','media_type':'video'} for i in range(20)]
        result = media.bilingual(materials,[2.4]*20,30.)
        self.assertGreater(len(result),11)
        self.assertLessEqual(len(result),20)

    def test_admission_freezes_owned_candidates_and_durations(self):
        video = importlib.import_module('content_domains.matrix_template_video')
        bilingual = importlib.import_module('content_domains.matrix_bilingual')
        materials = [{'sha256':f'{i:064x}','media_type':'video'} for i in range(3)]
        body = {'template_id':media.BILINGUAL,'top_text':'广州圈子','bottom_text':'交流成长','bgm':False}
        template = {'id':media.BILINGUAL,'font_selectable':False,'duration_mode':'narration'}
        from contextlib import ExitStack
        with ExitStack() as stack:
            for name, value in {'require_available':None,'public_templates':[template],
                                '_resolve_template_tuning':(None,None,None),
                                '_normalize_voiceover':{'text':'我在广州'},'_resolve_user_materials':materials}.items():
                stack.enter_context(mock.patch.object(video,name,return_value=value))
            stack.enter_context(mock.patch.object(bilingual,'ensure_ready'))
            stack.enter_context(mock.patch.object(media,'initial',return_value=([{'upload_id':'fixture','media_type':'video'}]*3,[8.]*3)))
            stack.enter_context(mock.patch.object(video,'_request',side_effect=lambda method,path,payload,**kw:{'payload':dict(payload)}))
            result = video.validate_payload(body,'alice')
        self.assertEqual(materials,result['user_materials'])
        self.assertEqual([8.]*3,result['_matrix_material_durations'])
        self.assertTrue(result['_matrix_auto_materials'])
        self.assertEqual('owned_public',result['material_policy'])
        self.assertNotIn('_matrix_material_durations',video._provider_payload(result))

    def test_auto_selection_is_reverified_by_existing_owner_hash_upload_path(self):
        video = importlib.import_module('content_domains.matrix_template_video')
        candidate = [{'upload_id':'vid_'+'a'*32,'media_type':'video'}]
        with mock.patch.object(video, '_read_user_upload', side_effect=ValueError('expired')):
            with self.assertRaisesRegex(ValueError, '已过期'):
                video._resolve_user_materials(candidate,'alice',video_only=True)


if __name__ == '__main__': unittest.main()
