# -*- coding: utf-8 -*-
"""Seedream（火山方舟 Ark）生图引擎。

下列常量/行为全部来自 2026-07-10 对 ark.cn-beijing.volces.com 的线上实测，不是文档推断：

  * 端点 POST /api/v3/images/generations（社区 README 写的 /images/generate 是笔误）
  * model id 取自本账号 GET /models：
        doubao-seedream-5-0-260128      （name=doubao-seedream-5-0，标准）
        doubao-seedream-5-0-pro-260628  （name=doubao-seedream-5-0-pro）
  * size 只吃「宽x高」像素串，且有硬下限 3686400 px
        1024x1024 / 1152x2048 → 400「image size must be at least 3686400 pixels」
        1440x2560 / 1920x1920 / 2928x1264 / 4688x2000 / 4096x4096 → 200
  * 图生图 image 必须是 data URI：
        "data:image/png;base64,xxx" → 200
        裸 base64                   → 400「invalid url specified」
  * response_format=b64_json 可用（省掉一次去 TOS 的下载）
  * 内容审核失败 → 400 OutputImageSensitiveContentDetected（官方不计费 → 应退点）
"""
import base64
import importlib
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

image = importlib.import_module("content_domains.image")
points = importlib.import_module("content_domains.points")

RATIOS = ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]


def _px(size):
    w, h = (int(x) for x in size.split("x"))
    return w, h, w * h


class SizeTests(unittest.TestCase):
    def test_every_ratio_variant_quality_lands_inside_window(self):
        """像素总数必须落在 [下限, 按型号的上限] 内，否则 Ark 400。
        Pro 上限 4624220 远低于标准版 —— 线上 3 单 Pro 高清正是拿了 9.4M 像素而挂掉。"""
        for variant, cap in image.SEEDREAM_MAX_PIXELS.items():
            for r in RATIOS:
                for q in ("std", "hd"):
                    w, h, px = _px(image._seedream_size(r, q, variant))
                    self.assertGreaterEqual(px, image.SEEDREAM_MIN_PIXELS, "%s/%s/%s" % (variant, r, q))
                    self.assertLessEqual(px, cap, "%s/%s/%s = %dx%d" % (variant, r, q, w, h))

    def test_dimensions_are_multiples_of_16(self):
        for variant in image.SEEDREAM_MAX_PIXELS:
            for r in RATIOS:
                for q in ("std", "hd"):
                    w, h, _ = _px(image._seedream_size(r, q, variant))
                    self.assertEqual((w % 16, h % 16), (0, 0), "%s/%s/%s" % (variant, r, q))

    def test_known_good_sizes_match_live_probes(self):
        """这些尺寸线上实测返回 200 且回显同样尺寸。"""
        self.assertEqual(image._seedream_size("9:16", "std"), "1440x2560")
        self.assertEqual(image._seedream_size("1:1", "std"), "1920x1920")
        self.assertEqual(image._seedream_size("9:16", "hd", "pro"), "1600x2848")   # 4556800 px → 200
        self.assertEqual(image._seedream_size("21:9", "hd", "pro"), "3280x1408")   # 4618240 px → 200（最贴边）

    def test_pro_hd_never_exceeds_its_own_cap(self):
        """回归守卫：1712x2704 = 4629248 px 线上实测被 400 拒。"""
        cap = image.SEEDREAM_MAX_PIXELS["pro"]
        for r in RATIOS:
            _, _, px = _px(image._seedream_size(r, "hd", "pro"))
            self.assertLessEqual(px, cap, r)

    def test_unknown_variant_uses_most_conservative_cap(self):
        """未知型号宁可出图小一点，也不要 400。"""
        strict = min(image.SEEDREAM_MAX_PIXELS.values())
        for r in RATIOS:
            _, _, px = _px(image._seedream_size(r, "hd", "future-model"))
            self.assertLessEqual(px, strict, r)

    def test_hd_is_larger_than_std_within_each_variant(self):
        for variant in image.SEEDREAM_MAX_PIXELS:
            for r in RATIOS:
                _, _, spx = _px(image._seedream_size(r, "std", variant))
                _, _, hpx = _px(image._seedream_size(r, "hd", variant))
                self.assertGreater(hpx, spx, "%s/%s" % (variant, r))

    def test_ratio_is_approximately_preserved(self):
        for variant in image.SEEDREAM_MAX_PIXELS:
            for r in RATIOS:
                rw, rh = (int(x) for x in r.split(":"))
                for q in ("std", "hd"):
                    w, h, _ = _px(image._seedream_size(r, q, variant))
                    self.assertAlmostEqual(w / h, rw / rh, delta=0.03, msg="%s/%s/%s" % (variant, r, q))

    def test_garbage_ratio_falls_back_not_crash(self):
        for bad in ("", None, "abc", "0:0", "1:", "-3:4"):
            for variant in ("std", "pro"):
                _, _, px = _px(image._seedream_size(bad, "hd", variant))
                self.assertGreaterEqual(px, image.SEEDREAM_MIN_PIXELS)
                self.assertLessEqual(px, image.SEEDREAM_MAX_PIXELS[variant])


class _Capture:
    """替身 _post：记录调用参数，返回一张假图的 url。"""
    def __init__(self):
        self.calls = []
    def __call__(self, path, data, ctype, base=None, key=None, proxy=True):
        self.calls.append({"path": path, "body": json.loads(data), "ctype": ctype,
                           "base": base, "key": key, "proxy": proxy})
        return {"data": [{"url": "https://tos.example/img.png", "size": "1920x1920"}]}


class RequestShapeTests(unittest.TestCase):
    def setUp(self):
        self.cap = _Capture()
        self.p = patch.object(image, "_post", self.cap); self.p.start()
        self.k = patch.object(image, "ARK_API_KEY", "test-key"); self.k.start()
        self.f = patch.object(image, "_seedream_fetch", lambda u, **kw: b"PNGBYTES"); self.f.start()

    def tearDown(self):
        for p in (self.p, self.k, self.f):
            p.stop()

    def test_endpoint_and_direct_connection(self):
        image._seedream_one("m", "p", "1920x1920", None)
        c = self.cap.calls[0]
        self.assertEqual(c["path"], "/images/generations")   # 不是 /images/generate
        self.assertEqual(c["base"], image.ARK_BASE)
        self.assertFalse(c["proxy"])  # 关键：火山在国内，必须绕过进程级 HTTPS_PROXY(mihomo/法兰克福)

    def test_text2img_body(self):
        image._seedream_one("mymodel", "画只猫", "1440x2560", None)
        b = self.cap.calls[0]["body"]
        self.assertEqual(b["model"], "mymodel")
        self.assertEqual(b["prompt"], "画只猫")
        self.assertEqual(b["size"], "1440x2560")
        self.assertFalse(b["watermark"])
        self.assertNotIn("image", b)     # 文生图不带 image

    def test_output_format_png_is_explicit(self):
        """不指定 output_format 时 Ark 默认吐 JPEG，会和 .png 文件名/Content-Type 对不上。"""
        image._seedream_one("m", "p", "1920x1920", None)
        self.assertEqual(self.cap.calls[0]["body"]["output_format"], "png")

    def test_never_requests_b64_json(self):
        """回归守卫：PNG 的 b64_json 响应体 4~5MB，实测 IncompleteRead；
        而 POST 非幂等，重试 = 重新出图 = 重复计费。只能走 url。"""
        image._seedream_one("m", "p", "1920x1920", None)
        self.assertEqual(self.cap.calls[0]["body"]["response_format"], "url")

    def test_img2img_uses_data_uri_not_bare_base64(self):
        """裸 base64 会被 Ark 判成 URL 并 400 invalid url specified。"""
        image._seedream_one("m", "p", "1920x1920", "QUJD")
        b = self.cap.calls[0]["body"]
        self.assertEqual(b["image"], "data:image/png;base64,QUJD")


class FetchRetryTests(unittest.TestCase):
    """下载可以安全重试（不重新出图、不重复计费）；生成不行。"""

    def test_retries_incomplete_read_then_succeeds(self):
        import http.client
        seen = {"n": 0}

        class _R:
            def __enter__(self_in):
                seen["n"] += 1
                if seen["n"] == 1:
                    raise http.client.IncompleteRead(b"half")
                return self_in
            def __exit__(self_in, *a):
                return False
            def read(self_in):
                return b"PNGBYTES"

        with patch.object(image, "_NOPROXY") as np, patch.object(image.time, "sleep", lambda *_: None):
            np.open.return_value = _R()
            self.assertEqual(image._seedream_fetch("u"), b"PNGBYTES")
        self.assertEqual(seen["n"], 2)

    def test_gives_up_with_clear_error(self):
        with patch.object(image, "_NOPROXY") as np, patch.object(image.time, "sleep", lambda *_: None):
            np.open.side_effect = urllib.error.URLError("down")
            with self.assertRaises(ValueError) as ctx:
                image._seedream_fetch("u", tries=2)
        self.assertIn("下载失败", str(ctx.exception))

    def test_download_failure_does_not_repost(self):
        """下载重试期间绝不再发 POST —— 否则每次重试都重新计费。"""
        cap = _Capture()
        with patch.object(image, "_post", cap), patch.object(image, "ARK_API_KEY", "k"), \
             patch.object(image, "_seedream_fetch", side_effect=ValueError("下载失败")):
            with self.assertRaises(ValueError):
                image._seedream_one("m", "p", "1920x1920", None)
        self.assertEqual(len(cap.calls), 1)   # 只出图一次


class ErrorMappingTests(unittest.TestCase):
    def _http(self, code, payload):
        import io
        return urllib.error.HTTPError("u", code, "err", {}, io.BytesIO(json.dumps(payload).encode()))

    def test_sensitive_content_becomes_friendly_business_error(self):
        e = self._http(400, {"error": {"code": "OutputImageSensitiveContentDetected", "message": "x"}})
        err = image._seedream_error(e)
        self.assertIsInstance(err, ValueError)
        self.assertIn("内容审核未通过", str(err))
        self.assertNotIn("OutputImage", str(err))   # 不把内部错误码抖给用户

    def test_input_sensitive_also_mapped(self):
        e = self._http(400, {"error": {"code": "InputImageSensitiveContentDetected", "message": "x"}})
        self.assertIn("内容审核未通过", str(image._seedream_error(e)))

    def test_other_errors_keep_code_and_message(self):
        e = self._http(400, {"error": {"code": "InvalidParameter", "message": "size too small"}})
        s = str(image._seedream_error(e))
        self.assertIn("黄雀引擎 1 400", s)
        self.assertIn("size too small", s)

    def test_unparseable_body_does_not_crash(self):
        import io
        e = urllib.error.HTTPError("u", 500, "err", {}, io.BytesIO(b"<html>"))
        self.assertIn("黄雀引擎 1 500", str(image._seedream_error(e)))


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.cap = _Capture()
        self.p = patch.object(image, "_post", self.cap); self.p.start()
        self.k = patch.object(image, "ARK_API_KEY", "test-key"); self.k.start()
        self.w = patch.object(image, "OUT_DIR", Path(__file__).parent); self.w.start()
        self.u = patch.object(image, "public_url", lambda fn, ct: "/x/" + fn); self.u.start()
        self.f = patch.object(image, "_seedream_fetch", lambda u, **kw: b"PNGBYTES"); self.f.start()
        self._written = []

    def tearDown(self):
        for f in self._written:
            try: f.unlink()
            except Exception: pass
        for p in (self.p, self.k, self.w, self.u, self.f):
            p.stop()

    def _run(self, **kw):
        payload = {"prompt": "p", "provider": "seedream"}
        payload.update(kw)
        out = image.gen_image(payload)
        self._written += [Path(__file__).parent / f for f in out["files"]]
        return out

    def test_default_variant_is_standard_model(self):
        out = self._run()
        self.assertEqual(out["model"], image.SEEDREAM_MODELS["std"])
        self.assertEqual(out["variant"], "std")

    def test_pro_variant_selects_pro_model(self):
        out = self._run(variant="pro")
        self.assertEqual(out["model"], image.SEEDREAM_MODELS["pro"])
        self.assertEqual(out["variant"], "pro")

    def test_unknown_variant_falls_back_to_standard(self):
        self.assertEqual(self._run(variant="lite")["model"], image.SEEDREAM_MODELS["std"])

    def test_mask_rejected_seedream_has_no_inpaint(self):
        """用**真 PNG** 蒙版：格式无可挑剔，也照样被拒 —— 拒的是「引擎没有 inpaint」。
        （传假字节的话，会先被扣点前的魔数校验拦下，就测不到这个不变量了。）"""
        with self.assertRaises(ValueError) as ctx:
            self._run(mask=base64.b64encode(PNG1).decode())
        self.assertIn("局部修改", str(ctx.exception))

    def test_count_capped_to_max_n(self):
        out = self._run(count=9)
        self.assertEqual(out["count"], image.SEEDREAM_MAX_N)
        self.assertEqual(len(self.cap.calls), image.SEEDREAM_MAX_N)  # 每张一次调用

    def test_missing_key_raises_before_any_call(self):
        with patch.object(image, "ARK_API_KEY", ""):
            with self.assertRaises(ValueError):
                image.gen_image({"prompt": "p", "provider": "seedream"})
        self.assertEqual(self.cap.calls, [])

    def test_img2img_mode_and_reference_passed(self):
        ref = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 32).decode()   # 需过 _seedream_check_ref 魔数校验
        out = self._run(image=ref)
        self.assertEqual(out["mode"], "img2img")
        self.assertEqual(self.cap.calls[0]["body"]["image"], "data:image/png;base64," + ref)

    def test_pro_hd_size_stays_within_pro_cap(self):
        """线上挂掉的正是这条路径：gen_image 没把 variant 传给 _seedream_size，
        Pro 高清拿到 9.4M 像素 → 400 image area must be at most 4624220 pixels。"""
        cap = image.SEEDREAM_MAX_PIXELS["pro"]
        for r in ("1:1", "9:16", "16:9", "21:9", "4:5"):
            self.cap.calls[:] = []
            self._run(variant="pro", quality="hd", ratio=r)
            w, h, px = _px(self.cap.calls[0]["body"]["size"])
            self.assertLessEqual(px, cap, "%s → %dx%d" % (r, w, h))
            self.assertGreaterEqual(px, image.SEEDREAM_MIN_PIXELS, r)

    def test_std_hd_still_gets_full_resolution(self):
        """修 Pro 不能把标准版也一起压小。"""
        self._run(variant="std", quality="hd", ratio="9:16")
        _, _, px = _px(self.cap.calls[0]["body"]["size"])
        self.assertGreater(px, image.SEEDREAM_MAX_PIXELS["pro"])


PNG1 = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
JPG1 = b"\xff\xd8\xff\xe0" + b"\0" * 32
WEBP1 = b"RIFF" + b"\0\0\0\0" + b"WEBP" + b"\0" * 24


class RefImageGuardTests(unittest.TestCase):
    """坏参考图送到 Ark 会回 HTTP 500『internal error』（实测），看起来像我们的故障。
    本地先验：给人话错误、且不白花一次上游往返。"""

    def test_accepts_png_jpeg_webp(self):
        for raw in (PNG1, JPG1, WEBP1):
            image._seedream_check_ref(base64.b64encode(raw).decode())   # 不抛即通过

    def test_none_or_empty_is_noop(self):
        image._seedream_check_ref(None)
        image._seedream_check_ref("")

    def test_rejects_non_image_bytes(self):
        with self.assertRaises(ValueError) as ctx:
            image._seedream_check_ref(base64.b64encode(b"not an image at all").decode())
        self.assertIn("格式不支持", str(ctx.exception))

    def test_rejects_oversized_reference(self):
        big = base64.b64encode(PNG1 + b"\0" * (image.SEEDREAM_MAX_REF_BYTES + 1)).decode()
        with self.assertRaises(ValueError) as ctx:
            image._seedream_check_ref(big)
        self.assertIn("太大", str(ctx.exception))

    def test_guard_runs_before_any_upstream_call(self):
        cap = _Capture()
        with patch.object(image, "_post", cap), patch.object(image, "ARK_API_KEY", "k"):
            with self.assertRaises(ValueError):
                image.gen_image({"prompt": "p", "provider": "seedream",
                                 "image": base64.b64encode(b"garbage").decode()})
        self.assertEqual(cap.calls, [])   # 一次上游调用都没发出


class FrontendParamSpaceTests(unittest.TestCase):
    """banana.html 在 Seedream 下能发出的全部参数组合，都不该产生参数类失败。
    比例只有 4 个（9:16 / 1:1 / 16:9 / 3:4），清晰度 2 档，数量 1~2。
    下列尺寸已用『坏图探针』在线上逐一验证 size 被 Ark 接受（0 成本，未生成图片）。"""

    FRONTEND_RATIOS = ["9:16", "1:1", "16:9", "3:4"]

    def test_all_frontend_combos_inside_window(self):
        for variant, cap in image.SEEDREAM_MAX_PIXELS.items():
            for r in self.FRONTEND_RATIOS:
                for q in ("std", "hd"):
                    w, h, px = _px(image._seedream_size(r, q, variant))
                    self.assertGreaterEqual(px, image.SEEDREAM_MIN_PIXELS, "%s/%s/%s" % (variant, r, q))
                    self.assertLessEqual(px, cap, "%s/%s/%s" % (variant, r, q))

    def test_count_never_exceeds_generator_cap(self):
        for n in (0, 1, 2, 3, 99, None):
            body = {"provider": "seedream", "quality": "hd", "count": n}
            self.assertLessEqual(points.cost_of("image", body) // 12, image.SEEDREAM_MAX_N)

    def test_mask_cannot_reach_seedream(self):
        """前端只对 gpt 显示局部修改；后端也硬拒，双保险。
        蒙版用真 PNG，确保拒绝理由是「引擎没有 inpaint」而非格式不合法。"""
        with patch.object(image, "ARK_API_KEY", "k"):
            with self.assertRaises(ValueError) as ctx:
                image.gen_image({"prompt": "p", "provider": "seedream",
                                 "mask": base64.b64encode(PNG1).decode()})
        self.assertIn("局部修改", str(ctx.exception))


class CostConsistencyTests(unittest.TestCase):
    """扣点数量上限必须与真实出图数量上限一致，否则超收。"""

    def test_seedream_cost_cap_matches_generator_cap(self):
        body = {"provider": "seedream", "quality": "hd", "count": 9}
        self.assertEqual(points.cost_of("image", body), 12 * image.SEEDREAM_MAX_N)

    def test_zelong2_and_xiaole_no_longer_overcharge(self):
        """回归守卫：gen_image 封 2 张，cost_of 曾按 4 张扣点。"""
        for provider in ("zelong", "zelong2", "xiaole"):
            body = {"provider": provider, "quality": "hd", "count": 4}
            self.assertEqual(points.cost_of("image", body), 12 * 2, provider)

    def test_gpt_still_allows_four(self):
        """守的是「gpt 数量上限仍为 4」，不是它的单价（单价见 test_image_pricing.py）。
        原来写死 12*4，gpt 高清改价成 15 后这条会误报，本质上是把价格耦合进了数量测试。"""
        gpt_hd = points.IMAGE_BASE_COST["openai"]["hd"]
        self.assertEqual(points.cost_of("image", {"provider": "openai", "quality": "hd", "count": 4}), gpt_hd * 4)
        self.assertEqual(points.cost_of("image", {"provider": "openai", "quality": "hd", "count": 9}), gpt_hd * 4)

    def test_seedream_variant_and_quality_tiers(self):
        # 2×2：型号(5.0标准 std / 5.0pro pro) × 清晰度(标准 std / 高清 hd)（kongli 2026-07-15）
        def c(variant, q):
            return points.cost_of("image", {"provider": "seedream", "variant": variant, "quality": q, "count": 1})
        self.assertEqual(c("std", "std"), 8)    # 5.0 标准 · 标准
        self.assertEqual(c("std", "hd"), 12)    # 5.0 标准 · 高清
        self.assertEqual(c("pro", "std"), 15)   # 5.0 Pro · 标准
        self.assertEqual(c("pro", "hd"), 20)    # 5.0 Pro · 高清

    def test_seedream_defaults_to_std_variant(self):
        # 不传 variant → 按 5.0 标准算（和前端 seedreamVariant 缺省一致）
        self.assertEqual(points.cost_of("image", {"provider": "seedream", "quality": "std", "count": 1}), 8)
        self.assertEqual(points.cost_of("image", {"provider": "seedream", "quality": "hd", "count": 1}), 12)


class PostRetryTests(unittest.TestCase):
    """出图 POST 非幂等 —— 重发 = 重新出图 = 重复计费。

    只有 429（限流：请求被拒、确定没出图）能安全重试。5xx / 超时 / 连接错误时
    Ark 可能已经出图并计费，一律不重发，直接失败退点，由用户决定要不要再来一次。
    （通用 _retry 会重试 5xx 和超时，所以生成路径不能用它。）
    """

    def _run(self, effects):
        calls = []

        def fake_post(*_a, **_kw):
            calls.append(1)
            eff = effects[len(calls) - 1]
            if isinstance(eff, Exception):
                raise eff
            return eff

        with patch.object(image, "_post", fake_post), \
             patch.object(image, "ARK_API_KEY", "k"), \
             patch.object(image, "_seedream_fetch", lambda u, **kw: b"PNGBYTES"), \
             patch.object(image.time, "sleep", lambda _s: None):
            try:
                return image._seedream_one("m", "p", "1920x1920", None), len(calls), None
            except Exception as err:          # 测试要看抛了什么
                return None, len(calls), err

    @staticmethod
    def _http(code, body=b'{"error":{"code":"TooManyRequests","message":"rate"}}'):
        import io
        return urllib.error.HTTPError("u", code, "err", {}, io.BytesIO(body))

    def test_5xx_is_not_retried_no_double_charge(self):
        """500 时上游可能已出图并计费 → 绝不重发。"""
        _out, n, err = self._run([self._http(500)])
        self.assertEqual(1, n, "5xx 不能重发 POST，否则重复计费")
        self.assertIsNotNone(err)

    def test_gateway_5xx_is_not_retried(self):
        _out, n, err = self._run([self._http(502)])
        self.assertEqual(1, n)
        self.assertIsNotNone(err)

    def test_timeout_is_not_retried(self):
        """超时 = 请求已发出，上游可能已出图 → 绝不重发。"""
        _out, n, err = self._run([TimeoutError("read timeout")])
        self.assertEqual(1, n, "超时不能重发 POST，否则重复计费")
        self.assertIsNotNone(err)

    def test_429_is_retried_then_succeeds(self):
        """429 确定未出图 → 可以安全重试。"""
        ok = {"data": [{"url": "https://example.com/a.png"}]}
        out, n, err = self._run([self._http(429), ok])
        self.assertIsNone(err)
        self.assertEqual(2, n, "429 应退避重试")
        self.assertEqual(b"PNGBYTES", out)

    def test_429_burst_absorbed_by_more_tries(self):
        """Ark 并发上限约 4~5，10 路突发时后几路连续 429；加大 tries 应能等到槽位吸收掉。"""
        ok = {"data": [{"url": "https://example.com/a.png"}]}
        effects = [self._http(429)] * 6 + [ok]   # 连 6 次 429 后成功
        with patch.object(image, "SEEDREAM_429_TRIES", 8):
            out, n, err = self._run(effects)
        self.assertIsNone(err)
        self.assertEqual(7, n)               # 6 次退避后第 7 次成功
        self.assertEqual(b"PNGBYTES", out)

    def test_429_exhausted_raises_not_loops(self):
        with patch.object(image, "SEEDREAM_429_TRIES", 3):
            _out, n, err = self._run([self._http(429), self._http(429), self._http(429)])
        self.assertEqual(3, n)               # 到 tries 上限即抛，不无限循环
        self.assertIsNotNone(err)

    def test_429_only_retried_never_other_codes(self):
        """确认非幂等安全：只有 429 重试，5xx/超时绝不重发(否则重复计费)。"""
        for eff in (self._http(500), self._http(503), TimeoutError("t")):
            _out, n, err = self._run([eff])
            self.assertEqual(1, n)
            self.assertIsNotNone(err)

    def test_set_limit_exceeded_fails_fast_no_retry(self):
        """SetLimitExceeded=账号用量上限/安全体验模式，模型已暂停 → 重试无用，立刻失败(实测 246s×10 全败)。"""
        paused = self._http(429, b'{"error":{"code":"SetLimitExceeded","message":"...paused...Safe Experience Mode..."}}')
        _out, n, err = self._run([paused])
        self.assertEqual(1, n, "已暂停的 429 不能重试，白占 worker")
        self.assertIsInstance(err, ValueError)
        self.assertIn("用量上限", str(err))


if __name__ == "__main__":
    unittest.main()
