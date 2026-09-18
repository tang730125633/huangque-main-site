"""Exercise the real validator/publisher; only the HTTP transport is faked."""
import json
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from tests import test_provider_config_loop as loop

pc = loop.pc


class ProviderProbeTests(unittest.TestCase):
    setUp = loop.SeedreamLoopTest.setUp
    tearDown = loop.SeedreamLoopTest.tearDown

    @staticmethod
    def response(status=200):
        response = MagicMock()
        response.__enter__.return_value.status = status
        return response

    def validate(self, draft, result):
        # A reachable root does not imply that /models authenticated the key.
        with patch('urllib.request.urlopen', side_effect=[self.response(), result]) as transport:
            result = pc.validate_draft(loop.TARGET, draft['seq'], actor='tester')
        self.assertEqual(transport.call_count, 2)
        self.assertTrue(all(call.args[0].method == 'GET' for call in transport.call_args_list))
        self.assertTrue(transport.call_args_list[-1].args[0].full_url.endswith('/models'))
        return result

    def draft(self):
        return pc.save_draft(loop.TARGET, url=loop.ARK_URL + '/wrong',
                             secret=loop.SECRET_B, actor='tester')

    def test_http_errors_cannot_validate_or_publish(self):
        baseline = pc.pin(loop.TARGET)['version']
        for status in (301, 400, 401, 403, 404, 408, 429, 500, 502, 503):
            with self.subTest(status=status):
                draft = self.draft()
                error = urllib.error.HTTPError('https://example.invalid', status,
                                                'untrusted ' + loop.SECRET_B, {}, None)
                result = self.validate(draft, error)
                self.assertTrue(result['checks']['connection']['ok'])
                self.assertFalse(result['ok'])
                self.assertFalse(result['checks']['auth']['ok'])
                self.assertNotIn(loop.SECRET_B, json.dumps(result))
                with self.assertRaises(pc.NotVerified):
                    pc.publish(loop.TARGET, draft['seq'], baseline, 'failed-' + str(status), actor='tester')
                self.assertEqual(pc.active_version(loop.TARGET)['seq'], baseline)

    def test_successful_http_verification_can_publish(self):
        draft = self.draft()
        result = self.validate(draft, self.response(200))
        self.assertTrue(result['ok'])
        published = pc.publish(loop.TARGET, draft['seq'], None, 'success', actor='tester')
        self.assertEqual(published['seq'], draft['seq'])

    def test_old_false_positive_evidence_cannot_publish(self):
        for status in (404, 429, 500):
            with self.subTest(status=status):
                draft = self.draft()
                pc.record_evidence(loop.TARGET, draft['seq'], {
                    'ok': True, 'checks': {'connection': {'ok': True},
                    'auth': {'ok': True, 'status': status}}}, actor='old-validator')
                with self.assertRaises(pc.NotVerified):
                    pc.publish(loop.TARGET, draft['seq'], None, 'old-'+str(status), actor='tester')

    def test_failed_revalidation_revokes_previous_success(self):
        draft = self.draft()
        self.assertTrue(self.validate(draft, self.response())['ok'])
        error = urllib.error.HTTPError('https://example.invalid', 429, '', {}, None)
        self.assertFalse(self.validate(draft, error)['ok'])
        with self.assertRaises(pc.NotVerified):
            pc.publish(loop.TARGET, draft['seq'], None, 'stale-success', actor='tester')

    def test_transport_error_does_not_leak_key_or_publish(self):
        draft = self.draft()
        result = self.validate(draft, TimeoutError('sensitive ' + loop.SECRET_B))
        self.assertFalse(result['ok'])
        self.assertNotIn(loop.SECRET_B, json.dumps(result))
        with self.assertRaises(pc.NotVerified):
            pc.publish(loop.TARGET, draft['seq'], None, 'timeout', actor='tester')

    def test_root_connection_failure_never_attempts_auth(self):
        with patch('urllib.request.urlopen', side_effect=OSError('sensitive ' + loop.SECRET_B)) as transport:
            result = pc._http_probe(loop.ARK_URL, loop.SECRET_B)
        self.assertEqual(transport.call_count, 1)
        self.assertFalse(result['connection']['ok'])
        self.assertFalse(result['auth']['ok'])
        self.assertNotIn(loop.SECRET_B, json.dumps(result))

    def test_non_success_response_object_does_not_authenticate(self):
        for status in (199, 302, 404, 429, 500):
            with self.subTest(status=status):
                self.assertFalse(self.validate(self.draft(), self.response(status))['ok'])
