import http.server
import os
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from server import tikhub


class ChannelsDetailTest(unittest.TestCase):
    def test_retries_when_decode_key_is_missing(self):
        incomplete = {
            "id": "first",
            "objectDesc": {"media": [{"url": "https://wxapp.tc.qq.com/first"}]},
        }
        complete = {
            "id": "second",
            "objectDesc": {"media": [{
                "url": "https://wxapp.tc.qq.com/second",
                "urlToken": "&token=fresh",
                "decodeKey": "secret",
            }]},
        }

        with patch.object(tikhub, "_p", side_effect=[incomplete, complete]) as request:
            result = tikhub.ch_detail("sph-test")

        self.assertEqual(request.call_count, 2)
        self.assertEqual(result["play_url"], "https://wxapp.tc.qq.com/second&token=fresh")
        self.assertEqual(result["decode_key"], "secret")


class ChannelsTranscriptRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.det = {
            "platform": "channels",
            "id": "sph-test",
            "play_url": "https://wxapp.tc.qq.com/video?token=secret-old",
            "decode_key": "decode-old",
        }
        self.ssl_eof = urllib.error.URLError(
            ssl.SSLEOFError(8, "UNEXPECTED_EOF_WHILE_READING")
        )

    def _run(self, download_side_effect, **patches):
        stack = [
            patch.object(tikhub, "_ch_download_encrypted", side_effect=download_side_effect),
            patch.object(tikhub, "_ch_decrypt_file"),
            patch.object(tikhub, "_whisper", return_value="识别成功"),
            patch.object(tikhub.time, "sleep"),
        ]
        for target, value in patches.items():
            stack.append(patch.object(tikhub, target, value))
        entered = [item.start() for item in stack]
        try:
            return tikhub.transcript(self.det), entered
        finally:
            for item in reversed(stack):
                item.stop()

    def test_ssl_eof_retries_download_then_runs_asr_once(self):
        result, mocks = self._run([self.ssl_eof, None])

        self.assertEqual(result, {"text": "识别成功", "source": "asr"})
        self.assertEqual(mocks[0].call_count, 2)
        self.assertEqual(mocks[1].call_count, 1)
        self.assertEqual(mocks[2].call_count, 1)

    def test_two_tls_failures_refresh_signed_url_before_last_attempt(self):
        refreshed = dict(self.det, play_url="https://wxapp.tc.qq.com/video?token=secret-new",
                         decode_key="decode-new")
        with patch.object(tikhub, "ch_detail", return_value=refreshed) as refresh:
            result, mocks = self._run([self.ssl_eof, self.ssl_eof, None])

        self.assertEqual(result["text"], "识别成功")
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(mocks[0].call_args_list[2].args[0], refreshed["play_url"])
        self.assertEqual(mocks[1].call_args.args[1], refreshed["decode_key"])
        self.assertEqual(mocks[1].call_count, 1)
        self.assertEqual(mocks[2].call_count, 1)

    def test_expired_url_refreshes_before_second_attempt(self):
        expired = urllib.error.HTTPError(self.det["play_url"], 403, "Forbidden", {}, None)
        refreshed = dict(self.det, play_url="https://wxapp.tc.qq.com/video?token=fresh",
                         decode_key="decode-fresh")
        with patch.object(tikhub, "ch_detail", return_value=refreshed) as refresh:
            result, mocks = self._run([expired, None])

        self.assertEqual(result["text"], "识别成功")
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(mocks[0].call_args_list[1].args[0], refreshed["play_url"])
        self.assertEqual(mocks[1].call_args.args[1], refreshed["decode_key"])
        self.assertEqual(mocks[1].call_count, 1)
        self.assertEqual(mocks[2].call_count, 1)

    def test_refresh_outage_keeps_original_url_for_last_attempt(self):
        with patch.object(tikhub, "ch_detail", side_effect=RuntimeError("refresh unavailable")) as refresh:
            result, mocks = self._run([self.ssl_eof, self.ssl_eof, None])

        self.assertEqual(result["text"], "识别成功")
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(mocks[0].call_args_list[2].args[0], self.det["play_url"])
        self.assertEqual(mocks[1].call_count, 1)
        self.assertEqual(mocks[2].call_count, 1)

    def test_permanent_local_error_does_not_retry_or_run_asr(self):
        with patch.object(tikhub, "_ch_download_encrypted", side_effect=ValueError("文件超过上限")) as download, \
             patch.object(tikhub, "_ch_decrypt_file") as decrypt, \
             patch.object(tikhub, "_whisper") as whisper, \
             patch.object(tikhub.time, "sleep"):
            with self.assertRaises(tikhub.ChannelsDownloadError) as cm:
                tikhub.transcript(self.det)

        self.assertIn("文件超过上限", str(cm.exception))
        self.assertEqual(download.call_count, 1)
        decrypt.assert_not_called()
        whisper.assert_not_called()

    def test_certificate_verification_failure_does_not_retry(self):
        cert_error = urllib.error.URLError(
            ssl.SSLCertVerificationError(1, "certificate verify failed")
        )
        with patch.object(tikhub, "_ch_download_encrypted", side_effect=cert_error) as download, \
             patch.object(tikhub, "_ch_decrypt_file") as decrypt, \
             patch.object(tikhub, "_whisper") as whisper, \
             patch.object(tikhub.time, "sleep"):
            with self.assertRaises(tikhub.ChannelsDownloadError):
                tikhub.transcript(self.det)

        self.assertEqual(download.call_count, 1)
        decrypt.assert_not_called()
        whisper.assert_not_called()

    def test_total_deadline_exhaustion_stops_after_first_attempt(self):
        with patch.object(tikhub, "ASR_DL_DEADLINE", 0), \
             patch.object(tikhub, "_ch_download_encrypted", side_effect=TimeoutError("budget")) as download, \
             patch.object(tikhub, "_ch_decrypt_file") as decrypt, \
             patch.object(tikhub, "_whisper") as whisper, \
             patch.object(tikhub.time, "sleep") as sleep:
            with self.assertRaises(tikhub.ChannelsDownloadError) as cm:
                tikhub.transcript(self.det)

        self.assertIn("下载超时", str(cm.exception))
        self.assertEqual(download.call_count, 1)
        decrypt.assert_not_called()
        sleep.assert_not_called()
        whisper.assert_not_called()

    def test_exhausted_tls_retries_hide_signed_url_and_skip_asr(self):
        with patch.object(tikhub, "_ch_download_encrypted", side_effect=self.ssl_eof) as download, \
             patch.object(tikhub, "_ch_decrypt_file") as decrypt, \
             patch.object(tikhub, "ch_detail", return_value=self.det), \
             patch.object(tikhub, "_whisper") as whisper, \
             patch.object(tikhub.time, "sleep"), \
             patch("builtins.print") as output:
            with self.assertRaises(tikhub.ChannelsDownloadError) as cm:
                tikhub.transcript(self.det)

        rendered = " ".join(str(call) for call in output.call_args_list) + str(cm.exception)
        self.assertEqual(download.call_count, 3)
        decrypt.assert_not_called()
        whisper.assert_not_called()
        self.assertIn("自动恢复后仍未成功", str(cm.exception))
        self.assertNotIn("secret-old", rendered)
        self.assertNotIn("decode-old", rendered)

    def test_decrypt_failure_is_not_retried_or_mislabeled_as_asr(self):
        with patch.object(tikhub, "_ch_download_encrypted") as download, \
             patch.object(tikhub, "_ch_decrypt_file",
                          side_effect=tikhub.ChannelsDecryptError("视频号文件解密失败：空输出")) as decrypt, \
             patch.object(tikhub, "_whisper") as whisper:
            with self.assertRaises(tikhub.ChannelsDecryptError) as cm:
                tikhub.transcript(self.det)

        self.assertIn("解密失败", str(cm.exception))
        self.assertEqual(download.call_count, 1)
        self.assertEqual(decrypt.call_count, 1)
        whisper.assert_not_called()

    def test_subprocess_timeout_decrypts_once_without_refresh_or_redownload(self):
        timeout = subprocess.TimeoutExpired(["curl"], 180)
        with patch.object(tikhub, "_ch_download_encrypted") as download, \
             patch.object(tikhub.subprocess, "run", side_effect=timeout) as run, \
             patch.object(tikhub, "ch_detail") as refresh, \
             patch.object(tikhub, "_whisper") as whisper:
            with self.assertRaises(tikhub.ChannelsDecryptError) as caught:
                tikhub.transcript(self.det)

        self.assertIn("解密超时", str(caught.exception))
        self.assertNotIn("decode-old", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(run.call_count, 1)
        refresh.assert_not_called()
        whisper.assert_not_called()

    def test_asr_failure_happens_after_one_successful_download(self):
        with patch.object(tikhub, "_ch_download_encrypted") as download, \
             patch.object(tikhub, "_ch_decrypt_file") as decrypt, \
             patch.object(tikhub, "_whisper", side_effect=RuntimeError("provider failed")) as whisper:
            with self.assertRaises(tikhub.TikHubError) as cm:
                tikhub.transcript(self.det)

        self.assertIn("视频号语音识别失败", str(cm.exception))
        self.assertEqual(download.call_count, 1)
        self.assertEqual(decrypt.call_count, 1)
        self.assertEqual(whisper.call_count, 1)

    def test_real_trickle_download_obeys_absolute_deadline(self):
        class TrickleHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "20")
                self.end_headers()
                for _index in range(20):
                    try:
                        self.wfile.write(b"x")
                        self.wfile.flush()
                    except (
                        BrokenPipeError, ConnectionAbortedError,
                        ConnectionResetError,
                    ):
                        break
                    time.sleep(0.1)

            def log_message(self, _format, *_args):
                return

        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), TrickleHandler,
        )
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:%d/video.enc" % server.server_port
        try:
            with tempfile.TemporaryDirectory() as temp, patch.object(
                tikhub, "_OPENER", urllib.request.build_opener(
                    urllib.request.ProxyHandler({})
                ),
            ):
                target = os.path.join(temp, "video.enc")
                started = time.monotonic()
                with self.assertRaises(TimeoutError):
                    tikhub._ch_download_encrypted(
                        url, target, time.time() + 0.25,
                    )
                self.assertLess(time.monotonic() - started, 0.75)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
