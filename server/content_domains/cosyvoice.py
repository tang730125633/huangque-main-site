# -*- coding: utf-8 -*-
"""阿里百炼 CosyVoice 声音复刻 —— 替换豆包 megatts。

为什么不装 dashscope SDK：全站信条是「不引第三方 SDK、手搓 urllib」(gpt/gemini/ark/heygen
无一例外)。CosyVoice 合成走 WebSocket，标准库没有 ws 客户端，故手写一个最小实现
(_ws_synth，约 100 行 socket+帧)。协议、URL、帧格式全部来自 2026-07-10 对线上 SDK 的抓包，
并用纯 stdlib 在服务器实测跑通(合成 2s 出 MP3、跨云拉腾讯 COS 参考音频成功)。

两类音色、两个模型（模型跟着音色走）：
  * 公共音色 = CosyVoice 预置(longwan/longcheng/... 免费直用)，跑在 cosyvoice-v1
  * 个人音色 = create_voice 复刻出来的 voice_id(形如 cosyvoice-v3.5-plus-bailian-xxx)，
    跑在 cosyvoice-v3.5-plus
判据：voice_id 以复刻模型名打头 → 用复刻模型；否则视作预置 → 用 v1。

计费：坑位(create_voice)免费、上限 1000；只按合成字符计费(usage.characters)。
"""
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import time
import urllib.error
import urllib.request

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_HTTP = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization"
DASHSCOPE_WS_HOST = "dashscope.aliyuncs.com"
DASHSCOPE_WS_PATH = "/api-ws/v1/inference"

CLONE_MODEL = os.environ.get("COSY_CLONE_MODEL", "cosyvoice-v3.5-plus")   # 复刻音色用
PRESET_MODEL = os.environ.get("COSY_PRESET_MODEL", "cosyvoice-v1")        # 预置音色用
CLONE_PREFIX = os.environ.get("COSY_CLONE_PREFIX", "hq")                  # create_voice 的 voice 名前缀

# 4 个公共音色 → CosyVoice 预置(kongli 亲选)。合成时 provider_voice 存这些名字，走 PRESET_MODEL。
PUBLIC_VOICE_PRESETS = {
    "S_d21F8OR62": "longwan",       # 温柔女声（情感种草）
    "S_l8wE8OR62": "longxiaochun",  # 活力女声（广告推荐）
    "S_pa0E8OR62": "longcheng",     # 沉稳男声（知识口播）
    "S_xaUB8OR62": "longxiaoxia",   # 亲和女声（本地生活）
}


def enabled():
    return bool(DASHSCOPE_API_KEY)


def model_for_voice(voice):
    """合成时按音色反推模型：复刻 voice_id 打头是复刻模型名 → 复刻模型；否则当预置 → v1。"""
    return CLONE_MODEL if str(voice or "").startswith(CLONE_MODEL) else PRESET_MODEL


# ===================== 音色管理（HTTP） =====================
def _http(action, extra=None, timeout=40):
    payload = {"model": "voice-enrollment", "input": {"action": action}}
    if extra:
        payload["input"].update(extra)
    req = urllib.request.Request(
        DASHSCOPE_HTTP, data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + DASHSCOPE_API_KEY, "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = (e.read() or b"").decode("utf-8", "replace")[:300]
        raise RuntimeError("CosyVoice 音色接口失败: HTTP %s %s" % (e.code, detail)) from e


def create_voice(audio_url, prefix=None):
    """用公网可访问的参考音频 URL 复刻一个音色，返回 voice_id。
    阿里同步去拉这个 url，所以传短时效的 COS 预签名 URL 即可(实测跨云可达)。"""
    d = _http("create_voice", {"target_model": CLONE_MODEL,
                               "prefix": (prefix or CLONE_PREFIX), "url": audio_url})
    vid = ((d.get("output") or {}).get("voice_id") or "").strip()
    if not vid:
        raise RuntimeError("CosyVoice 未返回 voice_id: " + json.dumps(d, ensure_ascii=False)[:200])
    return vid


def voice_status(voice_id):
    """返回 (status, 原始条目)。status 为 OK / 训练中 / 不存在('')。"""
    d = _http("list_voice")
    for v in (d.get("output") or {}).get("voice_list") or []:
        if v.get("voice_id") == voice_id:
            return str(v.get("status") or ""), v
    return "", None


def delete_voice(voice_id):
    return _http("delete_voice", {"voice_id": voice_id})


# ===================== 合成（stdlib WebSocket） =====================
def _ws_connect(api_key, timeout):
    raw = socket.create_connection((DASHSCOPE_WS_HOST, 443), timeout=timeout)
    sock = ssl.create_default_context().wrap_socket(raw, server_hostname=DASHSCOPE_WS_HOST)
    sock.settimeout(timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall((
        "GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n"
        "Authorization: Bearer %s\r\n\r\n" % (DASHSCOPE_WS_PATH, DASHSCOPE_WS_HOST, key, api_key)
    ).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("CosyVoice WebSocket 握手连接关闭")
        buf += chunk
    head, _, leftover = buf.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode("latin1")
    if "101" not in status_line:
        raise RuntimeError("CosyVoice WebSocket 握手失败: " + status_line)
    accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
    if accept.lower() not in head.decode("latin1").lower():
        raise RuntimeError("CosyVoice WebSocket Accept 校验失败")
    return sock, leftover


def _ws_send(sock, data, opcode=0x1):
    fin_op = 0x80 | opcode
    n = len(data)
    if n < 126:
        header = struct.pack("!BB", fin_op, 0x80 | n)
    elif n < 65536:
        header = struct.pack("!BBH", fin_op, 0x80 | 126, n)
    else:
        header = struct.pack("!BBQ", fin_op, 0x80 | 127, n)
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    sock.sendall(header + mask + masked)


def _ws_frames(sock, leftover):
    buf = leftover

    def fill(n):
        nonlocal buf
        while len(buf) < n:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("CosyVoice WebSocket 连接关闭")
            buf += chunk

    while True:
        fill(2)
        opcode = buf[0] & 0x0F
        ln = buf[1] & 0x7F
        idx = 2
        if ln == 126:
            fill(4); ln = struct.unpack("!H", buf[2:4])[0]; idx = 4
        elif ln == 127:
            fill(10); ln = struct.unpack("!Q", buf[2:10])[0]; idx = 10
        fill(idx + ln)
        payload = buf[idx:idx + ln]
        buf = buf[idx + ln:]
        yield opcode, payload


def synth(voice, text, fmt="mp3", sample_rate=22050, rate=1.0, pitch=1.0, volume=50, timeout=60):
    """合成一段语音，返回音频字节。model 按音色自动选(预置/复刻)。
    rate 语速(0.5~2)、pitch 音调(0.5~2)、volume 音量(0~100)——与抓包看到的参数名一致。"""
    if not DASHSCOPE_API_KEY:
        raise ValueError("CosyVoice 未配置（DASHSCOPE_API_KEY）")
    text = (text or "").strip()
    if not text:
        raise ValueError("配音文案不能为空")
    model = model_for_voice(voice)
    params = {"voice": voice, "format": fmt, "sample_rate": sample_rate,
              "rate": max(0.5, min(2.0, float(rate))),
              "pitch": max(0.5, min(2.0, float(pitch))),
              "volume": max(0, min(100, int(volume)))}
    sock, leftover = _ws_connect(DASHSCOPE_API_KEY, timeout)
    try:
        task_id = os.urandom(16).hex()
        _ws_send(sock, json.dumps({
            "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {"model": model, "task_group": "audio", "task": "tts",
                        "function": "SpeechSynthesizer", "input": {}, "parameters": params},
        }).encode())
        frames = _ws_frames(sock, leftover)
        for op, pl in frames:
            if op == 0x1 and json.loads(pl)["header"]["event"] == "task-started":
                break
            if op == 0x8:
                raise RuntimeError("CosyVoice 合成未启动即关闭")
        _ws_send(sock, json.dumps({
            "header": {"action": "continue-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {"input": {"text": text}}}).encode())
        _ws_send(sock, json.dumps({
            "header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {"input": {}}}).encode())
        audio = bytearray()
        for op, pl in frames:
            if op == 0x2:                    # binary = 音频块
                audio += pl
            elif op == 0x1:
                ev = json.loads(pl)
                event = ev["header"]["event"]
                if event == "task-finished":
                    break
                if event == "task-failed":
                    raise RuntimeError("CosyVoice 合成失败: " + json.dumps(ev["header"], ensure_ascii=False)[:200])
            elif op == 0x8:
                break
        if not audio:
            raise RuntimeError("CosyVoice 合成返回为空")
        return bytes(audio)
    finally:
        try:
            sock.close()
        except Exception:
            pass
