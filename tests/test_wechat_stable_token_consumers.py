import json
import os
import urllib.parse
import unittest
from pathlib import Path
from unittest.mock import patch

from server import wechat_subscribe
from server import wechat_virtual_pay as virtual_pay


class StableTokenConsumerTests(unittest.TestCase):
    def setUp(self):
        virtual_pay._TOKEN_CACHE.update(value="", expires_at=0)

    def tearDown(self):
        virtual_pay._TOKEN_CACHE.update(value="", expires_at=0)

    def test_virtual_pay_uses_stable_token_without_force_refresh(self):
        with patch.dict(
            os.environ,
            {"WX_MP_APPID": "wx-test", "WX_MP_APPSECRET": "secret-test"},
            clear=True,
        ), patch.object(
            virtual_pay,
            "_json_request",
            return_value={"access_token": "shared-token", "expires_in": 7200},
        ) as request:
            self.assertEqual(virtual_pay.access_token(), "shared-token")

        url, body = request.call_args.args[:2]
        self.assertEqual(url, virtual_pay.API_BASE + "/cgi-bin/stable_token")
        self.assertEqual(
            json.loads(body),
            {
                "grant_type": "client_credential",
                "appid": "wx-test",
                "secret": "secret-test",
                "force_refresh": False,
            },
        )

    def test_xpay_retries_once_with_new_shared_token(self):
        responses = [
            {"errcode": 40001, "errmsg": "invalid token"},
            {},
        ]
        with patch.object(
            virtual_pay, "access_token", side_effect=["old-token", "new-token"]
        ) as token, patch.object(
            virtual_pay, "invalidate_access_token", return_value=True
        ) as invalidate, patch.object(
            virtual_pay, "_json_request", side_effect=responses
        ) as request:
            result = virtual_pay._xpay(
                "/xpay/query_order",
                {"openid": "openid-1", "env": 0, "order_id": "HQ1"},
                signed=False,
            )

        self.assertEqual(result, {})
        self.assertEqual(token.call_count, 2)
        invalidate.assert_called_once_with("old-token")
        urls = [call.args[0] for call in request.call_args_list]
        self.assertEqual(
            [urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["access_token"][0] for url in urls],
            ["old-token", "new-token"],
        )

    def test_subscribe_send_retries_once_with_new_shared_token(self):
        responses = [
            {"errcode": 40014, "errmsg": "invalid token"},
            {},
        ]
        with patch.object(
            wechat_subscribe.wechat_vpay,
            "access_token",
            side_effect=["old-token", "new-token"],
        ) as token, patch.object(
            wechat_subscribe.wechat_vpay,
            "invalidate_access_token",
            return_value=True,
        ) as invalidate, patch.object(
            wechat_subscribe, "_post_json", side_effect=responses
        ) as request:
            result = wechat_subscribe.send(
                "openid-1",
                "作品完成",
                "2026-07-27 22:00",
                tid="template-1",
            )

        self.assertEqual(result, {})
        self.assertEqual(token.call_count, 2)
        invalidate.assert_called_once_with("old-token")
        urls = [call.args[0] for call in request.call_args_list]
        self.assertEqual(
            [urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["access_token"][0] for url in urls],
            ["old-token", "new-token"],
        )

    def test_production_python_has_no_legacy_access_token_endpoint(self):
        server = Path(__file__).resolve().parents[1] / "server"
        offenders = []
        for path in server.rglob("*.py"):
            if '"/cgi-bin/token?' in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(server.parent)))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
