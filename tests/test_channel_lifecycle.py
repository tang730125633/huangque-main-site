import base64
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from server.content_domains import channel_manager as cm, channel_lifecycle as life


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        env=patch.dict(os.environ,{'HQ_CHANNEL_DB':self.tmp.name+'/channels.db',
            'CONTENT_JOB_DB':self.tmp.name+'/jobs.db',
            'HQ_PROVIDER_KEYS_MASTER_KEY':base64.urlsafe_b64encode(b'a'*32).decode()})
        env.start();self.addCleanup(env.stop)
        with closing(sqlite3.connect(os.environ['CONTENT_JOB_DB'])) as c:
            c.execute('CREATE TABLE jobs(id INTEGER,status TEXT,payload TEXT)');c.commit()
        self.body=dict(name='测试',adapter='openai_image',model='model',base_url='http://127.0.0.1:9999',
                       secret='private-secret',enabled=True,fixture={'prompt':'test'})
        self.ch=cm.save('admin',self.body)

    def act(self,action,version=None):
        result=life.mutate('operator',dict(id=self.ch['id'],version=version or self.ch['version'],action=action,reason='维护测试'))
        self.ch=result
        return result

    def mapping(self):
        return cm.save_mapping('admin',dict(kind='image',front='front',channel=self.ch['id'],enabled=True))

    def test_disabled_rejects_new_work_but_keeps_old_snapshot(self):
        self.mapping();binding=cm.capture('image',{'model':'front','prompt':'test'})['_channel_binding']
        snapshot=cm.version(binding['id'],binding['version'],True)
        self.act('disable')
        with self.assertRaisesRegex(ValueError,'停用'):cm.capture('image',{'model':'front','prompt':'test'})
        with self.assertRaisesRegex(ValueError,'停用'):cm.reserve(self.ch['id'],'connection')
        self.assertTrue(cm.reserve(self.ch['id'],'task','old-job',snapshot))
        self.assertEqual(cm.version(self.ch['id'],1,True)['secret'],'private-secret')

    def test_delete_restore_and_history(self):
        self.act('disable');self.act('delete')
        self.assertTrue(cm.overview()['items'][0]['_lifecycle']['deleted'])
        with self.assertRaisesRegex(ValueError,'回收站'):cm.save('admin',dict(self.body,id=self.ch['id'],version=self.ch['version']))
        with self.assertRaisesRegex(ValueError,'回收站'):self.mapping()
        self.act('restore');self.assertFalse(self.ch['enabled'])
        self.act('enable');self.assertTrue(self.ch['enabled'])
        self.assertEqual(cm.version(self.ch['id'],with_secret=True)['secret'],'private-secret')
        self.assertEqual(len(cm.overview()['items'][0]['history']),5)

    def test_enabled_cannot_delete(self):
        with self.assertRaisesRegex(ValueError,'先停用'):self.act('delete')

    def test_mapping_reference_and_compare_before_remove(self):
        m=self.mapping();self.act('disable')
        with self.assertRaisesRegex(ValueError,'映射引用'):self.act('delete')
        with self.assertRaisesRegex(ValueError,'映射已变化'):life.unmap('a',{'selector':'image:front','expected':dict(m,label='stale')})
        life.unmap('a',{'selector':'image:front','expected':m});self.act('delete')

    def test_disabled_backup_mapping_still_blocks_delete(self):
        other=cm.save('a',self.body)
        cm.save_mapping('a',dict(kind='image',front='front',channel=other['id'],backup=self.ch['id'],enabled=False))
        self.act('disable')
        with self.assertRaisesRegex(ValueError,'映射引用'):self.act('delete')

    def test_active_job_blocks_delete(self):
        with closing(sqlite3.connect(os.environ['CONTENT_JOB_DB'])) as c:
            c.execute('INSERT INTO jobs VALUES(1,?,?)',('pending',json.dumps({'_channel_binding':self.ch})));c.commit()
        self.act('disable')
        with self.assertRaisesRegex(ValueError,'未结束任务'):self.act('delete')

    def test_unknown_result_blocks_delete(self):
        rid=cm.reserve(self.ch['id'],'connection')
        with closing(cm.db()) as c:c.execute("UPDATE runs SET state='unknown' WHERE id=?",(rid,));c.commit()
        self.act('disable')
        with self.assertRaisesRegex(ValueError,'结果未知'):self.act('delete')

    def test_missing_job_store_fails_closed(self):
        self.act('disable')
        with patch.dict(os.environ,{'CONTENT_JOB_DB':self.tmp.name+'/missing.db'}):
            with self.assertRaisesRegex(ValueError,'暂不可读'):self.act('delete')

    def test_stale_revision_and_reason_validation(self):
        self.act('disable')
        with self.assertRaisesRegex(ValueError,'已被修改'):self.act('enable',1)
        with self.assertRaises(ValueError):life.mutate('a',dict(self.ch,action='enable',reason=''))
        self.assertFalse(cm.overview()['items'][0]['enabled'])

    def test_legacy_controls_block_scoped_work_only(self):
        life.mutate_legacy('a',dict(id='openai',version=0,action='disable',reason='维护测试'))
        for kind,payload in [('image',{}),('sora_video',{})]:
            with self.assertRaisesRegex(ValueError,'暂停新任务'):cm.capture(kind,payload)
        self.assertEqual(cm.capture('audio',{'text':'ok'}),{'text':'ok'})
        self.mapping();self.assertIn('_channel_binding',cm.capture('image',{'model':'front','prompt':'ok'}))
        life.mutate_legacy('a',dict(id='openai',version=1,action='enable',reason='维护结束'))
        self.assertEqual(cm.capture('image',{}),{})

    def test_legacy_cannot_delete_or_fake_support(self):
        for key,action in [('openai','delete'),('deepseek','disable')]:
            with self.assertRaises(ValueError):life.mutate_legacy('a',dict(id=key,version=0,action=action,reason='维护测试'))

    def test_legacy_change_is_version_checked(self):
        body=dict(id='xai',version=0,action='disable',reason='维护测试')
        life.mutate_legacy('a',body)
        with self.assertRaisesRegex(ValueError,'状态已变化'):life.mutate_legacy('b',body)
        self.assertEqual(cm.overview()['legacy_controls']['xai']['actor'],'a')

    def test_concurrent_operators_cannot_overwrite_the_same_revision(self):
        body=dict(self.ch,action='disable',reason='维护测试')
        def change(actor):
            try:return life.mutate(actor,body)
            except ValueError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(change,['a','b']))
        self.assertEqual(sum(result is not None for result in results),1)
        self.assertEqual(cm.overview()['items'][0]['version'],2)

    def test_legacy_alias_normalization_cannot_bypass_stop(self):
        life.mutate_legacy('a',dict(id='openai',version=0,action='disable',reason='维护测试'))
        for provider in [' OpenAI ', 'unknown-falls-back-to-openai']:
            with self.assertRaisesRegex(ValueError,'暂停新任务'):cm.capture('image',{'provider':provider})

    def test_disable_rejects_before_payment(self):
        from server.content_domains import jobs_store
        self.mapping();self.act('disable')
        calls=[]
        with self.assertRaises(jobs_store.PaidJobDeductError):
            jobs_store.create_paid_jobs(None,lambda *a:calls.append(a),None,'image','user',[(1,{'model':'front','prompt':'test'})],'owner')
        self.assertEqual(calls,[])


if __name__=='__main__':unittest.main()
