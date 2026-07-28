import socket
import unittest

from tools.lipsync_poc.adapters.http import (
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

    def test_private_result_is_rejected_before_network_open(self):
        calls = []

        def opener(request, timeout):
            calls.append((request, timeout))
            raise AssertionError("network opener must not be called")

        with self.assertRaises(ProviderHttpError):
            download_file(
                "https://provider.example/result.mp4",
                "unused-result.mp4",
                opener=opener,
                resolver=resolver_for("127.0.0.1"),
            )
        self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main()
