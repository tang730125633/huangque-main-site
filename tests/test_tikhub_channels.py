import unittest
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


if __name__ == "__main__":
    unittest.main()
