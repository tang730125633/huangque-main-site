# -*- coding: utf-8 -*-
"""/api/gen/image 的扣点前校验（#535）。

核心不变量：**清洗在前，严格校验在后**。

#505 用 _clean_b64 修过「剪贴板粘贴带中间换行、反推回填缺尾部 padding」——这些是
合法的前端产物。若在校验层直接 b64decode(validate=True)，它们会在扣点前被判成
「必须是合法 base64」而 400，等于把 #505/#483(Ctrl+V 粘贴) 无声推回去。

这个不变量此前没有任何测试守着，所以才会被无声改掉。
"""
import base64
import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

image = importlib.import_module("content_domains.image")


def _b64(data):
    return base64.b64encode(data).decode()


# 74 字节：74 % 3 == 2，base64 末尾必然带一个 "="，这样 rstrip("=") 才真的去掉 padding。
# （若长度恰好是 3 的倍数，编码后无 padding，"缺 padding" 的用例就成了空跑。）
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 66


class DirtyBase64StillAcceptedTests(unittest.TestCase):
    """真实前端会送来的「脏」base64，必须照常通过校验（#505 的场景）。"""

    def _validate(self, img):
        return image.validate_image_payload({"prompt": "p", "image": img})

    def test_clean_base64(self):
        self._validate(_b64(PNG))

    def test_base64_with_inner_newlines(self):
        """剪贴板粘贴（#483 的 Ctrl/⌘V）会带中间换行。"""
        raw = _b64(PNG)
        dirty = "\n".join(raw[i:i + 20] for i in range(0, len(raw), 20))
        self.assertIn("\n", dirty)
        self._validate(dirty)

    def test_base64_missing_padding(self):
        """反推回填 / 灵感跟创会缺尾部 padding。"""
        raw = _b64(PNG)
        self.assertTrue(raw.endswith("="), "样本本身要带 padding，否则本用例是空跑")
        self._validate(raw.rstrip("="))

    def test_data_url_prefix(self):
        self._validate("data:image/png;base64," + _b64(PNG))

    def test_data_url_with_newlines_and_no_padding(self):
        raw = _b64(PNG)
        self.assertTrue(raw.endswith("="))
        raw = raw.rstrip("=")
        dirty = "data:image/png;base64," + "\n".join(raw[i:i + 16] for i in range(0, len(raw), 16))
        self._validate(dirty)

    def test_mask_takes_same_path(self):
        raw = _b64(PNG)
        dirty = "\n".join(raw[i:i + 20] for i in range(0, len(raw), 20))
        image.validate_image_payload({"prompt": "p", "image": raw, "mask": dirty})

    def test_normalized_value_is_decodable(self):
        """校验后写回 body 的值必须是干净、可直接解码的 base64。"""
        raw = _b64(PNG).rstrip("=")
        out = self._validate("\n".join(raw[i:i + 12] for i in range(0, len(raw), 12)))
        self.assertEqual(PNG, base64.b64decode(out["image"], validate=True))   # 内容也要一致


class RealGarbageStillRejectedTests(unittest.TestCase):
    """清洗不是放行一切 —— 真正的垃圾仍要在扣点前挡住。"""

    def test_non_base64_characters(self):
        with self.assertRaises(ValueError):
            image.validate_image_payload({"prompt": "p", "image": "这不是 base64!!! @#$%"})

    def test_oversized_image_rejected(self):
        big = _b64(b"\x00" * (image.IMAGE_REF_MAX_BYTES + 1024))
        with self.assertRaises(ValueError) as cm:
            image.validate_image_payload({"prompt": "p", "image": big})
        self.assertIn("MB", str(cm.exception))


class PromptAndCountTests(unittest.TestCase):
    def test_empty_prompt_rejected(self):
        with self.assertRaises(ValueError):
            image.validate_image_payload({"prompt": "   "})

    def test_prompt_limit_is_generous(self):
        """Ark 实测吃得下 2 万字；2000 是没必要的收紧。"""
        self.assertGreaterEqual(image.IMAGE_PROMPT_MAX_CHARS, 8000)
        image.validate_image_payload({"prompt": "字" * 8000})

    def test_prompt_over_limit_rejected(self):
        with self.assertRaises(ValueError):
            image.validate_image_payload({"prompt": "字" * (image.IMAGE_PROMPT_MAX_CHARS + 1)})

    def test_count_clamped_to_upper_bound(self):
        """cost_of 按 count 扣点，count=100 会扣爆点，必须夹住。"""
        out = image.validate_image_payload({"prompt": "p", "count": 100})
        self.assertEqual(image.IMAGE_MAX_COUNT, out["count"])

    def test_count_lower_bound(self):
        self.assertEqual(1, image.validate_image_payload({"prompt": "p", "count": 0})["count"])

    def test_count_non_numeric_rejected(self):
        with self.assertRaises(ValueError):
            image.validate_image_payload({"prompt": "p", "count": "abc"})

    def test_body_must_be_dict(self):
        with self.assertRaises(ValueError):
            image.validate_image_payload("not a dict")


if __name__ == "__main__":
    unittest.main()
