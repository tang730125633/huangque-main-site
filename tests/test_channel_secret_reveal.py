import json
import pathlib
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

import tests.test_channel_manager as base
from server.content_domains import channel_manager as cm, safe_http
import server.admin_api as admin_api


def _local_resolver(host, port, type=0):
    if host in ('127.0.0.1', 'localhost'):
        return [(2, 1, 6, '', ('127.0.0.1', port))]
    return [(2, 1, 6, '', ('93.184.216.34', port))]


class ChannelSecretRevealTests(unittest.TestCase):
    def setUp(self):
        base.ChannelTests.setUp(self)
        real_validate = safe_http.validate_target
        self._resolver_patch = patch.object(
            safe_http, 'validate_target',
            side_effect=lambda url, proxy=False: real_validate(url, proxy=proxy, resolver=_local_resolver))
        self._resolver_patch.start()
        self.addCleanup(self._resolver_patch.stop)

        dbf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        dbf.close()
        self.admin_db = pathlib.Path(dbf.name)
        self.old_admin_db = admin_api.ADMIN_DB
        admin_api.ADMIN_DB = self.admin_db
        admin_api.init_db()
        self.addCleanup(lambda: self.admin_db.unlink(missing_ok=True))

        body = dict(self.body, name='乐创生图', adapter='lechuang_image', model='gpt-image-2',
                    base_url='https://api.lechuang.chat/api/v1', secret='sk-live-secret')
        self.ch = cm.save('admin', body)

    def tearDown(self):
        admin_api.ADMIN_DB = self.old_admin_db

    def test_reveal_returns_secret_once_and_writes_audit(self):
        result = admin_api.reveal_channel_secret('operator1', {'id': self.ch['id']})
        self.assertEqual(result['secret'], 'sk-live-secret')
        self.assertEqual(result['expires_in'], 5)
        self.assertEqual(result['id'], self.ch['id'])
        with closing(admin_api.db()) as conn:
            rows = conn.execute(
                "SELECT actor,action,target FROM admin_audit WHERE action='channel.secret.reveal'"
            ).fetchall()
        self.assertEqual([(r[0], r[1], r[2]) for r in rows],
                         [('operator1', 'channel.secret.reveal', self.ch['id'])])

    def test_reveal_rejects_missing_or_unknown_channel(self):
        with self.assertRaisesRegex(ValueError, '渠道编号'):
            admin_api.reveal_channel_secret('admin', {'id': ''})
        with self.assertRaisesRegex(ValueError, '渠道不存在'):
            admin_api.reveal_channel_secret('admin', {'id': 'no-such-channel'})
        with closing(admin_api.db()) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM admin_audit WHERE action='channel.secret.reveal'"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_reveal_event_visible_in_overview_without_secret(self):
        admin_api.reveal_channel_secret('admin', {'id': self.ch['id']})
        overview = admin_api.channel_workspace_overview()
        actions = [e['action'] for e in overview.get('legacy_events') or []]
        self.assertIn('channel.secret.reveal', actions)
        self.assertNotIn('sk-live-secret', json.dumps(overview))


if __name__ == '__main__':
    unittest.main()
