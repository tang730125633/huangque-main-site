# -*- coding: utf-8 -*-
"""检测延迟的状态码分类：把「地址已响应」和「连不上」分清楚。

背景：乐创 /api/v1、WaveSpeed /api/v3、DashScope 基础地址都不是业务接口，
无凭据 HEAD 会返回 404。页面只显示一个 HTTP 404，管理员会误判渠道不可用。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))

from content_domains import channel_latency


class LatencyClassifyTests(unittest.TestCase):
    def test_success(self):
        self.assertEqual(channel_latency._classify(200)[0], 'ok')
        self.assertEqual(channel_latency._classify(204)[0], 'ok')

    def test_address_answered_even_if_not_2xx(self):
        """404/401/403/405/5xx 都证明地址回应了，不能等同于不可用。"""
        for status, reason in ((404, 'not_found'), (401, 'rejected'), (403, 'rejected'),
                               (405, 'head_not_allowed'), (500, 'server_error'),
                               (502, 'server_error')):
            with self.subTest(status=status):
                self.assertEqual(channel_latency._classify(status)[0], reason)

    def test_messages_are_specific(self):
        self.assertIn('基础路径未找到', channel_latency._classify(404)[1])
        self.assertIn('无凭据探测被拒绝', channel_latency._classify(401)[1])
        self.assertIn('不支持 HEAD 探测', channel_latency._classify(405)[1])
        self.assertIn('远端返回服务错误', channel_latency._classify(502)[1])

    def test_redirect_and_other(self):
        self.assertEqual(channel_latency._classify(301)[0], 'redirect')
        self.assertEqual(channel_latency._headline(301), '已收到重定向')
        self.assertEqual(channel_latency._classify(429)[0], 'http_error')

    def test_headline_distinguishes_response_from_redirect(self):
        self.assertEqual(channel_latency._headline(200), '已收到响应')
        self.assertEqual(channel_latency._headline(404), '已收到响应')


if __name__ == '__main__':
    unittest.main()
