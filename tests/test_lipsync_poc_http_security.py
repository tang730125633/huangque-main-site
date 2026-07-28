import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.lipsync_poc.adapters.http import (
    PinnedHTTPSConnection,
    ProviderHttpError,
    _safe_download_url,
    download_file,
)


def resolver_for(*addresses):
    def resolve(host, port, **kwargs):
        return [
            (socket.AF_INET6 if ":" in address else socket.AF_INET,
             socket.SOCK_STREAM, 6, "", (address, port))
            for address in addresses
        ]
    return resolve


class LipsyncPocHttpSecurityTests(unittest.TestCase):
    def test_public_https_result_is_allowed(self):
        url = "https://provider.example/result.mp4"
        self.assertEqual(
            url,
            _safe_download_url(url, resolver_for("93.184.216.34")),
        )

    def test_private_or_mixed_dns_results_are_rejected(self):
        for addresses in (
            ("127.0.0.1",),
            ("10.0.0.8",),
            ("169.254.169.254",),
            ("::1",),
            ("93.184.216.34", "192.168.1.8"),
        ):
            with self.subTest(addresses=addresses):
                with self.assertRaises(ProviderHttpError) as raised:
                    _safe_download_url(
                        "https://provider.example/result.mp4",
                        resolver_for(*addresses),
                    )
                self.assertEqual(
                    "provider_result_url_forbidden",
                    raised.exception.code,
                )

    def test_malformed_port_is_safely_rejected(self):
        with self.assertRaises(ProviderHttpError) as raised:
            _safe_download_url(
                "https://provider.example:notaport/result.mp4",
                resolver_for("93.184.216.34"),
            )
        self.assertEqual(
            "provider_result_url_invalid",
            raised.exception.code,
        )

    def test_private_result_is_rejected_before_connection(self):
        calls = []

        def connection_factory(host, port, pinned_ip, timeout):
            calls.append((host, port, pinned_ip, timeout))
            raise AssertionError("connection must not be created")

        with self.assertRaises(ProviderHttpError):
            download_file(
                "https://provider.example/result.mp4",
                "unused-result.mp4",
                connection_factory=connection_factory,
                resolver=resolver_for("127.0.0.1"),
            )
        self.assertEqual([], calls)

    def test_download_connects_to_validated_ip_without_second_dns_lookup(self):
        resolver_calls = []
        connections = []

        def resolver(host, port, **kwargs):
            resolver_calls.append((host, port))
            if len(resolver_calls) > 1:
                return resolver_for("127.0.0.1")(host, port, **kwargs)
            return resolver_for("93.184.216.34")(host, port, **kwargs)

        class Response:
            status = 200
            reason = "OK"

            def read(self, size=-1):
                if getattr(self, "_read", False):
                    return b""
                self._read = True
                return b"video"

            def getheaders(self):
                return []

            def close(self):
                return None

        class Connection:
            def __init__(self, host, port, pinned_ip, timeout):
                connections.append((host, port, pinned_ip, timeout))
                self.request_args = None

            def request(self, method, target, body=None, headers=None):
                self.request_args = (method, target, body, headers)

            def getresponse(self):
                return Response()

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "result.mp4"
            result = download_file(
                "https://provider.example/result.mp4?token=secret",
                destination,
                resolver=resolver,
                connection_factory=Connection,
            )
            self.assertEqual(b"video", destination.read_bytes())
            self.assertEqual(5, result["size_bytes"])

        self.assertEqual(
            [("provider.example", 443)],
            resolver_calls,
        )
        self.assertEqual(
            ("provider.example", 443, "93.184.216.34"),
            connections[0][:3],
        )

    def test_redirect_response_is_rejected_without_following(self):
        class Response:
            status = 302
            reason = "Found"

            def read(self, size=-1):
                return b""

            def getheaders(self):
                return [("Location", "https://127.0.0.1/result.mp4")]

            def close(self):
                return None

        class Connection:
            def __init__(self, host, port, pinned_ip, timeout):
                pass

            def request(self, method, target, body=None, headers=None):
                pass

            def getresponse(self):
                return Response()

            def close(self):
                return None

        with self.assertRaises(ProviderHttpError) as raised:
            download_file(
                "https://provider.example/result.mp4",
                "unused-result.mp4",
                resolver=resolver_for("93.184.216.34"),
                connection_factory=Connection,
            )
        self.assertEqual(
            "provider_result_redirect_forbidden",
            raised.exception.code,
        )

    def test_default_connection_uses_pinned_ip_and_original_tls_host(self):
        raw_socket = Mock()
        raw_socket.getpeername.return_value = ("93.184.216.34", 443)
        tls_socket = Mock()
        tls_socket.getpeername.return_value = ("93.184.216.34", 443)
        context = Mock()
        context.wrap_socket.return_value = tls_socket

        with patch(
            "tools.lipsync_poc.adapters.http.ssl.create_default_context",
            return_value=context,
        ), patch(
            "tools.lipsync_poc.adapters.http.socket.create_connection",
            return_value=raw_socket,
        ) as create_connection:
            connection = PinnedHTTPSConnection(
                "provider.example",
                443,
                "93.184.216.34",
                30,
            )
            connection.connect()

        create_connection.assert_called_once_with(
            ("93.184.216.34", 443),
            30,
            None,
        )
        context.wrap_socket.assert_called_once_with(
            raw_socket,
            server_hostname="provider.example",
        )


if __name__ == "__main__":
    unittest.main()
