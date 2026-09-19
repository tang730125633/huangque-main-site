import unittest
from unittest.mock import Mock, patch

from server.content_domains import channel_latency as latency, safe_http


class ChannelLatencyTests(unittest.TestCase):
    def setUp(self):
        self.version = patch.object(latency.channel_manager, 'version', return_value={
            'id': 'one', 'version': 3, 'base_url': 'https://example.com/v1'})
        self.version_mock = self.version.start()
        self.addCleanup(self.version.stop)

    def probe(self, **kw):
        return latency.measure('admin', {'uid': 'managed:one', **kw}, lambda: [])

    def test_head_has_no_key_or_proxy_and_excludes_lookup_time(self):
        with patch.object(safe_http, 'request_bytes', return_value=204) as req, \
                patch.object(latency.time, 'monotonic', side_effect=[10, 10.125]):
            result = self.probe()
        self.assertEqual(result['latency_ms'], 125)
        self.assertEqual(result['http_status'], 204)
        self.assertEqual(result['state'], 'reachable')
        self.assertFalse(result['authentication_tested'])
        self.assertFalse(result['generation_tested'])
        req.assert_called_once_with('HEAD', 'https://example.com/v1', headers={},
                                    timeout=10, max_bytes=0, head_status_only=True)
        self.version_mock.assert_called_once_with('one')

    def test_http_errors_are_not_model_success(self):
        for status in (401, 404, 405, 429, 500):
            with self.subTest(status=status), patch.object(safe_http, 'request_bytes', return_value=status):
                result = self.probe()
                self.assertTrue(result['network_reachable'])
                self.assertEqual(result['state'], 'http_error')
                self.assertFalse(result['generation_tested'])

    def test_redirect_is_not_followed(self):
        with patch.object(safe_http, 'request_bytes', side_effect=safe_http.SafeHttpError('secret', 302)):
            result = self.probe()
        self.assertEqual(result['state'], 'redirect_blocked')
        self.assertNotIn('secret', str(result))

    def test_timeout_separate_from_http_status(self):
        error = safe_http.SafeHttpError('secret')
        error.__cause__ = TimeoutError()
        with patch.object(safe_http, 'request_bytes', side_effect=error):
            result = self.probe()
        self.assertEqual(result['state'], 'timeout')
        self.assertIsNone(result['http_status'])
        self.assertFalse(result['network_reachable'])

    def test_rejects_arbitrary_input_and_unknown_uid(self):
        for body in ({'uid': 'managed:one', 'url': 'https://attacker.example'},
                     {'uid': 'https://attacker.example'}, {'uid': 'legacy:absent'}):
            with self.subTest(body=body), self.assertRaises(ValueError), \
                    patch.object(safe_http, 'request_bytes') as req:
                latency.measure('admin', body, lambda: [])
            req.assert_not_called()

    def test_multisource_requires_explicit_choice(self):
        rows = [{'key': 'openai', 'env_base_url': 'https://a.example',
                 'pool_base_url': 'https://b.example'}]
        with patch.object(safe_http, 'request_bytes', return_value=200) as req:
            result = latency.measure('admin', {'uid': 'legacy:openai'}, lambda: rows)
            self.assertEqual(result['state'], 'unavailable')
            req.assert_not_called()
            selected = latency.measure('admin', {'uid': 'legacy:openai', 'source': 'pool'}, lambda: rows)
            self.assertEqual(selected['source'], 'pool')
            self.assertEqual(req.call_args.args[1], 'https://b.example')

    def test_registered_secret_bearing_url_is_not_requested(self):
        self.version_mock.return_value['base_url'] = 'https://example.com/?key=secret'
        with patch.object(safe_http, 'request_bytes') as req:
            result = self.probe()
        req.assert_not_called()
        self.assertNotIn('secret', str(result))

    def test_safe_http_head_reads_status_without_body_or_redirect(self):
        target = safe_http.Target('https', 'example.com', 443, 'example.com', '/', ('93.184.216.34',))
        response = Mock(status=401)
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(safe_http, 'validate_target', return_value=target):
            status = safe_http.request_bytes('HEAD', 'https://example.com',
                                            connection_factory=lambda *a: connection, head_status_only=True)
            self.assertEqual(status, 401)
            response.read.assert_not_called()
            response.status = 302
            with self.assertRaises(safe_http.SafeHttpError):
                safe_http.request_bytes('HEAD', 'https://example.com',
                                        connection_factory=lambda *a: connection, head_status_only=True)
        connection.close.assert_called()


if __name__ == '__main__':
    unittest.main()
