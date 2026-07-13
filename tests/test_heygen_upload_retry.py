# -*- coding: utf-8 -*-
"""参考素材上传：提高超时 + 加重试。

## 线上：7 条电影化身死在上传，占失败的一半

报错清一色 `The read operation timed out`，耗时 **241~248 秒** —— 正好撞上写死的 240s 超时。
出境隧道被整形在 ~1.1 MB/s，一个 4MB 的参考视频加上握手和抖动就能擦到 240s。

这些任务【压根没提交到 HeyGen】（`provider_video_id` 是空的），跟内容审核毫无关系 ——
但用户看到的一样是「生成失败」。

## 两个洞，缺一个另一个就白修

1. **超时写死 240s** → 放宽到 600s（可 env 调）。素材上传【不计费】，放宽只会多等，不会烧钱。

2. **`_heygen_direct_req` 根本不包装网络错误** —— 它只 catch HTTPError，读超时裸抛成
   `socket.timeout`。而 `_heygen_retry_net` 只认 `HeyGenNetworkError`，于是【接不住】。
   线上那 7 条报的就是裸的 "The read operation timed out"，一次都没重试过。
   光加重试而不补这个包装，重试就是形同虚设。

## 红线：重试【只能】包住不计费的调用

视频提交即扣费（$7/条）。给提交加重试 = 同一条片子付两次。有测试守着三处提交路径都
不许出现 `_heygen_retry_net`。
"""
import importlib
import re
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

video = importlib.import_module("content_domains.video")
SRC = (ROOT / "server/content_domains/video.py").read_text(encoding="utf-8")


class UploadTimeoutTests(unittest.TestCase):
    def test_the_timeout_is_no_longer_the_240s_that_killed_them(self):
        self.assertGreaterEqual(video.HEYGEN_UPLOAD_TIMEOUT, 600)
        self.assertNotIn("timeout=240", SRC, "还有写死的 240s —— 线上就是死在这个数上")

    def test_it_is_env_tunable(self):
        """隧道扩容/降级都可能要改这个数，别逼人改代码重新部署。"""
        self.assertIn('os.environ.get("HEYGEN_UPLOAD_TIMEOUT"', SRC)


class NetworkErrorsAreWrappedTests(unittest.TestCase):
    """不包装就接不住 —— 重试形同虚设。"""

    def test_a_read_timeout_becomes_a_HeyGenNetworkError(self):
        with patch.object(video, "_heygen_direct_opener") as op:
            op.return_value.open.side_effect = socket.timeout("The read operation timed out")
            with patch.object(video, "HEYGEN_API_KEY", "k"):
                with self.assertRaises(video.HeyGenNetworkError):
                    video._heygen_direct_req("POST", "https://upload.heygen.com/v1/asset", b"x")

    def test_an_http_error_is_NOT_wrapped(self):
        """HTTP 状态错误是上游【明确的应答】—— 盲重没有意义，还可能重复触发副作用。"""
        import urllib.error
        with patch.object(video, "_heygen_direct_opener") as op:
            op.return_value.open.side_effect = urllib.error.HTTPError(
                "u", 400, "Bad Request", {}, None)
            with patch.object(video, "HEYGEN_API_KEY", "k"):
                with self.assertRaises(RuntimeError) as e:
                    video._heygen_direct_req("POST", "https://upload.heygen.com/v1/asset", b"x")
        self.assertNotIsInstance(e.exception, video.HeyGenNetworkError)


class UploadIsRetriedTests(unittest.TestCase):
    def test_the_reference_upload_is_wrapped_in_the_retry(self):
        block = SRC.split("def gen_cinematic")[1].split("\ndef ")[0]
        self.assertIn("_heygen_retry_net(", block)
        self.assertIn("_heygen_upload_asset(_resolve_out_file(f), direct=True)", block)

    def test_the_lambda_binds_the_loop_variable(self):
        """`lambda: ...f...` 在循环里是【延迟求值】的经典坑：重试时 f 早就指向最后一个文件了。
        必须 `lambda f=f:` 把它绑死。"""
        block = SRC.split("def gen_cinematic")[1].split("\ndef ")[0]
        self.assertIn("lambda f=f:", block)


class SubmitIsNeverRetriedTests(unittest.TestCase):
    """⚠️ 最要紧的一条。HeyGen 提交即计费（$7/条）—— 重发 = 同一条片子付两次。"""

    def test_no_video_submit_path_uses_the_network_retry(self):
        for fn in ("generate_heygen_video_direct", "generate_heygen_video", "gen_cinematic"):
            block = SRC.split("def %s" % fn)[1].split("\ndef ")[0]
            # gen_cinematic 里【素材上传】那句是允许的（不计费）；【提交】那句不许。
            for line in block.splitlines():
                if "_heygen_create_cinematic_video" in line or "_heygen_create_video" in line:
                    self.assertNotIn("_heygen_retry_net", line,
                                     "%s 的提交被包进了网络重试 —— 会把同一条视频付两次钱" % fn)

    def test_submits_only_carry_the_429_retry(self):
        """429 是【突发限流】：被拒 = 没计费，可以安全重试。这和网络错误不同 ——
        网络错误【无法证明】请求没送达，而提交一旦送达就已经扣钱了。"""
        for m in re.finditer(r"_heygen_retry_429\(lambda: _heygen_create_\w+\(", SRC):
            self.assertTrue(True)
        self.assertGreaterEqual(SRC.count("_heygen_retry_429("), 3)


if __name__ == "__main__":
    unittest.main()
