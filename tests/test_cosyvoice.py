# -*- coding: utf-8 -*-
"""CosyVoice（阿里百炼）声音复刻 —— 替换豆包。

线上实测(2026-07-10，服务器直连百炼)：
  * 鉴权/create_voice/list_voice/delete 走 HTTP，坑位免费、上限 1000
  * 合成走 WebSocket(wss://dashscope.aliyuncs.com/api-ws/v1/inference)，纯 stdlib 手写客户端
    跑通：预置 longwan 1.5s、复刻音色 1.3s 出 MP3
  * 跨云：阿里 create_voice 能拉腾讯 COS 预签名 URL（Audio too short 即证明拉到了）
  * 模型跟音色走：预置→cosyvoice-v1，复刻→cosyvoice-v3.5-plus
"""
import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

cosyvoice = importlib.import_module("content_domains.cosyvoice")


class ModelRoutingTests(unittest.TestCase):
    def test_preset_voice_uses_v1(self):
        self.assertEqual(cosyvoice.model_for_voice("longwan"), cosyvoice.PRESET_MODEL)
        self.assertEqual(cosyvoice.model_for_voice("longcheng"), "cosyvoice-v1")

    def test_clone_voice_uses_clone_model(self):
        vid = "cosyvoice-v3.5-plus-bailian-55f8f2c3124e4754857ce79b51291d54"
        self.assertEqual(cosyvoice.model_for_voice(vid), cosyvoice.CLONE_MODEL)

    def test_empty_defaults_to_preset(self):
        self.assertEqual(cosyvoice.model_for_voice(""), cosyvoice.PRESET_MODEL)


class PublicPresetMapTests(unittest.TestCase):
    def test_four_public_voices_mapped(self):
        m = cosyvoice.PUBLIC_VOICE_PRESETS
        self.assertEqual(m["S_d21F8OR62"], "longwan")       # 温柔女声
        self.assertEqual(m["S_l8wE8OR62"], "longxiaochun")  # 活力女声
        self.assertEqual(m["S_pa0E8OR62"], "longcheng")     # 沉稳男声
        self.assertEqual(m["S_xaUB8OR62"], "longxiaoxia")   # 亲和女声


class EnabledGateTests(unittest.TestCase):
    def test_disabled_without_key(self):
        with patch.object(cosyvoice, "DASHSCOPE_API_KEY", ""):
            self.assertFalse(cosyvoice.enabled())

    def test_synth_raises_without_key(self):
        with patch.object(cosyvoice, "DASHSCOPE_API_KEY", ""):
            with self.assertRaises(ValueError):
                cosyvoice.synth("longwan", "你好")


class HttpShapeTests(unittest.TestCase):
    """create_voice/list/delete 的请求体与返回解析，不打网。"""

    def _fake_http(self, captured, response):
        def fake(action, extra=None, timeout=40):
            captured.append((action, extra))
            return response
        return fake

    def test_create_voice_body_and_parse(self):
        cap = []
        with patch.object(cosyvoice, "_http", self._fake_http(cap, {"output": {"voice_id": "cosyvoice-v3.5-plus-bailian-abc"}})), \
             patch.object(cosyvoice, "DASHSCOPE_API_KEY", "k"):
            vid = cosyvoice.create_voice("https://cos/ref.mp3", prefix="hq")
        action, extra = cap[0]
        self.assertEqual(action, "create_voice")
        self.assertEqual(extra["target_model"], cosyvoice.CLONE_MODEL)
        self.assertEqual(extra["url"], "https://cos/ref.mp3")
        self.assertEqual(extra["prefix"], "hq")
        self.assertEqual(vid, "cosyvoice-v3.5-plus-bailian-abc")

    def test_create_voice_missing_id_raises(self):
        with patch.object(cosyvoice, "_http", lambda *a, **k: {"output": {}}):
            with self.assertRaises(RuntimeError):
                cosyvoice.create_voice("https://cos/ref.mp3")

    def test_voice_status_finds_entry(self):
        resp = {"output": {"voice_list": [
            {"voice_id": "v1", "status": "OK"}, {"voice_id": "v2", "status": "pending"}]}}
        with patch.object(cosyvoice, "_http", lambda *a, **k: resp):
            self.assertEqual(cosyvoice.voice_status("v2")[0], "pending")
            self.assertEqual(cosyvoice.voice_status("nope")[0], "")


class WebSocketFramingTests(unittest.TestCase):
    """手写 WebSocket 帧编解码的正确性（RFC 6455）——协议错了合成拿不到音频。"""

    def test_client_frame_is_masked(self):
        sent = []

        class _Sock:
            def sendall(self, b): sent.append(b)

        cosyvoice._ws_send(_Sock(), b'{"a":1}')
        frame = sent[0]
        self.assertEqual(frame[0], 0x81)              # FIN + text opcode
        self.assertTrue(frame[1] & 0x80)              # MASK 位必须置(客户端帧强制 mask)
        mask = frame[2:6]
        unmasked = bytes(frame[6 + i] ^ mask[i % 4] for i in range(len(frame) - 6))
        self.assertEqual(unmasked, b'{"a":1}')

    def test_extended_length_encoding(self):
        sent = []

        class _Sock:
            def sendall(self, b): sent.append(b)

        cosyvoice._ws_send(_Sock(), b"x" * 200)       # 126~65535 → 2 字节长度
        self.assertEqual(sent[0][1] & 0x7F, 126)

    def test_frame_reader_splits_text_and_binary(self):
        # 构造服务端(未 mask)两帧：一个 text，一个 binary
        def server_frame(data, opcode):
            n = len(data)
            if n < 126:
                return bytes([0x80 | opcode, n]) + data
            return bytes([0x80 | opcode, 126]) + n.to_bytes(2, "big") + data

        stream = server_frame(b'{"ok":1}', 0x1) + server_frame(b"AUDIO", 0x2)

        class _Sock:
            def __init__(self, b): self.b = b
            def recv(self, n):
                chunk, self.b = self.b[:n], self.b[n:]
                return chunk

        frames = cosyvoice._ws_frames(_Sock(stream), b"")
        op1, pl1 = next(frames)
        op2, pl2 = next(frames)
        self.assertEqual((op1, pl1), (0x1, b'{"ok":1}'))
        self.assertEqual((op2, pl2), (0x2, b"AUDIO"))


class AudioVoiceMappingTests(unittest.TestCase):
    """audio._cosy_voice_for：库里的 provider_voice → CosyVoice 能用的 voice。"""

    def setUp(self):
        self.audio = importlib.import_module("content_domains.audio")

    def test_public_code_maps_to_preset(self):
        self.assertEqual(self.audio._cosy_voice_for("S_d21F8OR62"), "longwan")
        self.assertEqual(self.audio._cosy_voice_for("S_pa0E8OR62"), "longcheng")

    def test_clone_id_passthrough(self):
        vid = "cosyvoice-v3.5-plus-bailian-abc"
        self.assertEqual(self.audio._cosy_voice_for(vid), vid)

    def test_old_doubao_voice_rejected_with_guidance(self):
        for old in ("vip_S_xxx", "S_notmapped"):
            with self.assertRaises(ValueError) as ctx:
                self.audio._cosy_voice_for(old)
            self.assertIn("重新复刻", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
