import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from server.content_domains import task_termination as termination, jobs_store


class TerminationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'jobs.db'
        with closing(self.db()) as c:
            c.execute('''CREATE TABLE jobs(id INTEGER PRIMARY KEY, kind TEXT, username TEXT,
                cost INTEGER, status TEXT, payload TEXT, result TEXT, error TEXT,
                updated_at INTEGER, refunded INTEGER DEFAULT 0)''')
            c.execute("INSERT INTO jobs VALUES(1,'matrix_template_video','customer',5,'pending','{}',NULL,NULL,0,0)")
            c.execute('ALTER TABLE jobs ADD COLUMN created_at INTEGER')
            c.execute("UPDATE jobs SET created_at=CAST(strftime('%s','now') AS INTEGER)")
            c.commit()

    def tearDown(self):
        self.temp.cleanup()

    def db(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def update(self, status='running', payload=None):
        with closing(self.db()) as c:
            c.execute('UPDATE jobs SET status=?,payload=? WHERE id=1', (status, json.dumps(payload or {})))
            c.commit()

    def cancel(self):
        return termination.request(self.db, 1, 'operator', '用户要求取消')

    def test_pending_cannot_be_claimed_and_refund_is_durable(self):
        result = self.cancel()['termination']
        self.assertEqual(result['platform_state'], 'stopped')
        self.assertEqual(result['remote_state'], 'not_submitted')
        self.assertEqual(result['refund_state'], 'pending')
        self.assertFalse(jobs_store.claim_running(self.db, 1))
        self.assertFalse(jobs_store.refund_once(self.db, 1, 'customer', 5, lambda *_: False))
        with closing(self.db()) as c:
            self.assertEqual(termination.describe(c, 1)['termination']['refund_state'], 'pending')
        refund = mock.Mock(return_value=True)
        jobs_store.refund_once(self.db, 1, 'customer', 5, refund)
        self.cancel()
        jobs_store.refund_once(self.db, 1, 'customer', 5, refund)
        refund.assert_called_once_with('customer', 5)

    def test_running_waits_for_worker_ack_and_preserves_late_provider_id(self):
        self.update()
        with termination.scope(1, self.db):
            self.assertEqual(self.cancel()['termination']['platform_state'], 'stopping')
            termination.provider_submitted('late-provider-order')
            with self.assertRaises(termination.TaskTerminated):
                termination.check()
        with closing(self.db()) as c:
            info = termination.describe(c, 1)['termination']
            self.assertEqual(info['platform_state'], 'stopping')
            self.assertEqual(info['provider_id'], 'late-provider-order')
            self.assertEqual(info['remote_state'], 'unconfirmed')
        termination.acknowledge(self.db, 1)
        with closing(self.db()) as c:
            self.assertEqual(termination.describe(c, 1)['termination']['platform_state'], 'stopped')

    def test_cancel_wins_prevents_late_completion(self):
        self.update()
        self.cancel()
        self.assertFalse(jobs_store.set_terminal(self.db, 1, 'done', result={'url': 'late'}))

    def test_completion_wins_prevents_refund(self):
        self.update()
        self.assertTrue(jobs_store.set_terminal(self.db, 1, 'done', result={}))
        with self.assertRaises(ValueError):
            self.cancel()
        with closing(self.db()) as c:
            self.assertIsNone(termination.get(c, 1))
            self.assertEqual(c.execute('SELECT refunded FROM jobs').fetchone()[0], 0)

    def test_concurrent_cancel_keeps_one_audit_record(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda _: self.cancel(), range(5)))
        self.assertEqual(len({r['termination']['requested_at'] for r in results}), 1)
        self.assertEqual(sum(1 for r in results if r.get('created')), 1)
        again = termination.request(self.db, 1, 'different', '不同的原因')
        self.assertFalse(again.get('created'))
        self.assertEqual(again['termination']['actor'], 'operator')

    def test_ended_but_supported_task_reports_concrete_reason(self):
        self.update()
        self.assertTrue(jobs_store.set_terminal(self.db, 1, 'done', result={}))
        with closing(self.db()) as c:
            described = termination.describe(c, 1)
        self.assertFalse(described['can_terminate'])
        self.assertIn('已结束', described['unavailable_reason'])

    def test_unsupported_and_recovery_submission_are_truthful(self):
        for payload in ({'mode': 'timeline'}, {'_channel_binding': {'id': 1}}):
            self.update(payload=payload)
            with self.assertRaises(ValueError):
                self.cancel()
        self.update(status='pending', payload={'_matrix_runtime': {'phase': 'submitting'}})
        self.assertEqual(self.cancel()['termination']['remote_state'], 'unconfirmed')

    def test_reason_validation_and_unrelated_kind(self):
        for reason in ('', 'x', 'x' * 201):
            with self.assertRaises(ValueError):
                termination.request(self.db, 1, 'operator', reason)
        with closing(self.db()) as c:
            c.execute("UPDATE jobs SET kind='image'")
            c.commit()
        with self.assertRaises(ValueError):
            self.cancel()

    def test_own_subprocess_is_killed_after_cancellation(self):
        self.update()
        real_popen = subprocess.Popen
        started = threading.Event()
        processes = []
        def start(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            started.set()
            return process
        def cancel_after_start():
            if started.wait(5):
                self.cancel()
        worker = threading.Thread(target=cancel_after_start)
        worker.start()
        with mock.patch.object(termination.subprocess, 'Popen', side_effect=start):
            with self.assertRaises(termination.TaskTerminated), termination.scope(1, self.db):
                termination.run_process([sys.executable, '-c', 'import time;time.sleep(30)'], timeout=10)
        worker.join(5)
        self.assertIsNotNone(processes[0].poll())

    def test_endpoint_checks_role_and_records_authenticated_actor(self):
        from types import SimpleNamespace
        from server.content_domains import core
        body = json.dumps({'job_id': 1, 'reason': '用户要求取消', 'actor': 'forged'}).encode()
        handler = SimpleNamespace(path='/api/gen/admin/tasks/terminate',
            headers={'Authorization': 'Bearer test-only', 'Content-Length': str(len(body))},
            rfile=io.BytesIO(body), _send=mock.Mock())
        with mock.patch.object(core, 'verify', return_value={'username': 'customer', 'role': 'user'}):
            core.H._do_POST(handler)
        self.assertEqual(handler._send.call_args.args[0], 403)
        with closing(self.db()) as c:
            self.assertIsNone(termination.get(c, 1))
        with mock.patch.object(core, 'verify', return_value={'username': 'actual-admin', 'role': 'admin'}), \
             mock.patch.object(core, 'jdb', self.db), mock.patch.object(core.threading, 'Thread'):
            core.H._do_POST(handler)
        self.assertEqual(handler._send.call_args.args[0], 200)
        with closing(self.db()) as c:
            self.assertEqual(termination.get(c, 1)['actor'], 'actual-admin')

    def test_cancel_during_submission_prevents_polling_and_delivery(self):
        from server.content_domains import core, matrix_template_video as matrix
        self.update()
        def submit(*args, **kwargs):
            self.cancel()
            return {'job_id': 'a' * 32, 'status': 'pending'}
        with mock.patch.object(core, 'jdb', self.db), \
             mock.patch.object(matrix, 'validate_payload', return_value={'template_id': 'native-bold'}), \
             mock.patch.object(matrix, '_request', side_effect=submit) as request, \
             mock.patch.object(matrix, '_download') as download:
            with self.assertRaises(termination.TaskTerminated):
                matrix.generate({'_job_id': 1, '_username': 'customer'})
        request.assert_called_once()
        download.assert_not_called()
        with closing(self.db()) as c:
            self.assertEqual(termination.get(c, 1)['provider_id'], 'a' * 32)


if __name__ == '__main__':
    unittest.main()
