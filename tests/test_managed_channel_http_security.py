import socket
import unittest

from server.content_domains import safe_http


def resolver_for(mapping, calls):
    def resolve(host, port, **_kwargs):
        calls.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (mapping[host], port))]
    return resolve


class ManagedChannelHttpSecurityTests(unittest.TestCase):
    def test_private_or_mixed_target_is_rejected_before_connection(self):
        for addresses in (['127.0.0.1'], ['169.254.169.254'],
                          ['93.184.216.34', '10.0.0.8']):
            def resolver(host, port, **_kwargs):
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port))
                        for ip in addresses]
            with self.subTest(addresses=addresses), self.assertRaises(ValueError):
                safe_http.validate_target('https://provider.example/result',
                                          resolver=resolver)

    def test_request_pins_one_resolution_and_rejects_redirect(self):
        calls = []
        made = []

        class Response:
            status = 302
            def read(self, _size=-1): return b''
            def close(self): pass

        class Connection:
            def __init__(self, host, port, pinned_ip, timeout):
                made.append((host, port, pinned_ip, timeout))
            def request(self, *_args, **_kwargs): pass
            def getresponse(self): return Response()
            def close(self): pass

        with self.assertRaisesRegex(safe_http.SafeHttpError, '重定向'):
            safe_http.request_bytes(
                'GET', 'https://provider.example/result',
                resolver=resolver_for({'provider.example':'93.184.216.34'}, calls),
                connection_factory=Connection,
            )
        self.assertEqual(calls, [('provider.example', 443)])
        self.assertEqual(made[0][:3], ('provider.example', 443, '93.184.216.34'))

    def test_proxy_and_target_are_both_resolved_once_and_pinned(self):
        calls = []
        made = []

        class Response:
            status = 200
            def read(self, _size=-1): return b'{}'
            def close(self): pass

        class ProxyConnection:
            def __init__(self, target, proxy, timeout):
                made.append((target, proxy, timeout))
            def request(self, *_args, **_kwargs): pass
            def getresponse(self): return Response()
            def close(self): pass

        raw = safe_http.request_bytes(
            'GET', 'https://provider.example/model',
            proxy='http://proxy.example:8080',
            resolver=resolver_for({
                'provider.example':'93.184.216.34',
                'proxy.example':'93.184.216.35',
            }, calls),
            connection_factory=ProxyConnection,
        )
        self.assertEqual(raw, b'{}')
        self.assertEqual(calls, [('provider.example', 443), ('proxy.example', 8080)])
        self.assertEqual(made[0][0].addresses, ('93.184.216.34',))
        self.assertEqual(made[0][1].addresses, ('93.184.216.35',))


if __name__ == '__main__':
    unittest.main()
