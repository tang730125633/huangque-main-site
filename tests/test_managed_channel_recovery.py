import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from server.content_domains import startup_recovery


class ManagedChannelRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / 'jobs.db'
        with closing(self.jdb()) as connection:
            connection.execute(
                'CREATE TABLE jobs(id INTEGER PRIMARY KEY,username TEXT,cost INTEGER,'
                'kind TEXT,payload TEXT,status TEXT,owner TEXT,error TEXT,updated_at INTEGER)'
            )
            connection.commit()

    def tearDown(self):
        self.folder.cleanup()

    def jdb(self):
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        return connection

    def insert(self, job_id):
        with closing(self.jdb()) as connection:
            connection.execute(
                'INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',
                (job_id, 'u', 10, 'image',
                 json.dumps({'_channel_binding':{'id':'c','version':1}}),
                 'running', 'content', None, int(time.time())),
            )
            connection.commit()

    def run_recovery(self, state):
        terminal = []
        refunds = []
        with patch('server.content_domains.channel_manager.task_recovery_state',
                   return_value=state):
            handled = startup_recovery.reclaim_orphaned_running(
                jdb=self.jdb, service_owner='content', domains=lambda: (),
                set_terminal=lambda *args, **kwargs: terminal.append((args, kwargs)),
                refund_once=lambda *args: refunds.append(args),
                mark_video_asset_failed=lambda *args: None,
                requeue_job=lambda job_id: startup_recovery.requeue_running_job(
                    self.jdb, job_id),
                logger=lambda *_args, **_kwargs: None,
            )
        return handled, terminal, refunds

    def test_unknown_and_unavailable_are_held_without_refund(self):
        for index, state in enumerate(('unknown', 'running', 'passed', 'unavailable'), 1):
            self.insert(index)
            handled, terminal, refunds = self.run_recovery(state)
            self.assertEqual(handled, 0)
            self.assertEqual(terminal, [])
            self.assertEqual(refunds, [])
            with closing(self.jdb()) as connection:
                self.assertEqual(connection.execute(
                    'SELECT status FROM jobs WHERE id=?', (index,)).fetchone()[0],
                    'running')

    def test_queued_or_not_yet_reserved_is_safely_requeued(self):
        for index, state in enumerate(('queued', 'absent'), 20):
            self.insert(index)
            handled, terminal, refunds = self.run_recovery(state)
            self.assertEqual(handled, 1)
            self.assertEqual(terminal, [])
            self.assertEqual(refunds, [])
            with closing(self.jdb()) as connection:
                self.assertEqual(connection.execute(
                    'SELECT status FROM jobs WHERE id=?', (index,)).fetchone()[0],
                    'pending')


if __name__ == '__main__':
    unittest.main()
