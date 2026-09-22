"""后台「直接切换接单渠道」——隔离验证基座。

隔离什么（全部指向临时目录 / 本地进程，绝不碰生产）：

  * 渠道库      HQ_CHANNEL_DB            → 临时 SQLite，绝不开 HQ_CHANNEL_STORE=postgres
  * 运行台账    HQ_OBSERVABILITY_DB      → 临时 SQLite
  * 产物目录    CONTENT_OUT              → 临时目录
  * 凭据        HQ_PROVIDER_KEYS_MASTER_KEY → 本次运行随机生成的虚拟主密钥
  * 供应商      MockProvider             → 127.0.0.1 上的假供应商，记录每一次调用

被测代码是仓库里真实的 channel_manager / channel_runtime / channel_parameters，
不做任何打桩替换 generate()，因此「任务到底走了哪个渠道」是真实 HTTP 行为。
"""

import base64
import io
import json
import os
import sys
import sqlite3
import tempfile
import threading
import unittest
import zlib
import struct
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
# 本文件在 <仓库>/tests/isolated/ 下，仓库根目录由位置推导；可用 HQ_REPO 覆盖。
# 不写死任何机器上的绝对路径。
REPO = Path(os.environ.get('HQ_REPO') or Path(__file__).resolve().parents[2])
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'server'))


# --------------------------------------------------------------------------- 假图片
def png_bytes(width, height, seed=0):
    """生成一张合法 PNG（让 channel_runtime 的 PIL 成品核验能真通过）。"""
    row = b'\x00' + bytes([(seed * 7 + x) % 256 for x in range(width * 3)])
    raw = row * height
    def chunk(tag, data):
        body = tag + data
        return struct.pack('>I', len(data)) + body + struct.pack('>I', zlib.crc32(body) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 6))
            + chunk(b'IEND', b''))


# --------------------------------------------------------------------------- 假供应商
class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):        # 静音
        pass

    # -- 记录 -----------------------------------------------------------------
    def _record(self, method, body):
        entry = {
            'method': method,
            'path': self.path,
            'authorization': self.headers.get('Authorization') or '',
            'api_key_header': self.headers.get('x-goog-api-key') or '',
            'idempotency_key': self.headers.get('Idempotency-Key') or '',
            'body': body,
        }
        self.server.calls.append(entry)
        if body and isinstance(body, dict):
            size = (body.get('size') or (body.get('input') or {}).get('size') or '')
            if size:
                self.server.image_size = size
        return entry

    def _json(self, payload, code=200):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _png(self):
        size = self.server.image_size or '1024x1024'
        try:
            width, height = (int(x) for x in size.split('x'))
        except ValueError:
            width = height = 1024
        data = png_bytes(width, height, seed=self.server.port % 97)
        self.send_response(200)
        self.send_header('Content-Type', 'image/png')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- 路由 -----------------------------------------------------------------
    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(length) or b'{}')
        except ValueError:
            body = {}
        self._record('POST', body)
        # base_url 里带 /v1，所以按「协议段 + 端点」判断，不写成完整路径前缀。
        if self.path.rstrip('/').endswith('/videos'):
            # Sora（OpenAI videos API）：POST /videos → {id, status}
            self._json({'id': 'vid-%s-1' % self.server.port, 'status': 'queued',
                        'model': body.get('model'), 'seconds': str(body.get('seconds') or ''),
                        'size': str(body.get('size') or '')})
        elif ('/lechuang-image/' in self.path or '/lechuang-video-' in self.path) and self.path.rstrip('/').endswith('/generations'):
            self._json({'data': {'request_id': 'req-lechuang-0001', 'status': 'queued'}})
        elif '/openai-image/' in self.path and self.path.rstrip('/').endswith('/generations'):
            size = self.server.image_size or '1024x1024'
            width, height = (int(x) for x in size.split('x'))
            self._json({'data': [{'b64_json': base64.b64encode(png_bytes(width, height, 3)).decode()}],
                        'model': body.get('model')})
        else:
            self._json({'error': 'unknown path'}, 404)

    def do_GET(self):
        self._record('GET', None)
        if self.path.rstrip('/').endswith('/content'):
            # Sora 成品下载：返回真实可解码的 mp4，让 ffprobe 成品校验真通过
            data = io.open(str(HERE / 'fixtures' / 'tiny.mp4'), 'rb').read()
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if '/videos/' in self.path:
            # Sora 状态查询：直接 completed，避免测试等待
            vid = self.path.rstrip('/').rsplit('/', 1)[-1]
            self._json({'id': vid, 'status': 'completed', 'model': 'sora-2-pro',
                        'seconds': '4', 'size': '720x1280'})
            return
        if '/lechuang-video-' in self.path and '/generations/' in self.path:
            self._json({'data': {'status':'succeeded', 'output': {'videos':[
                {'url':'http://127.0.0.1:%d/file.mp4' % self.server.port}]}}})
        elif self.path.endswith('/file.mp4'):
            data = (HERE / 'fixtures' / 'tiny.mp4').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type','video/mp4')
            self.send_header('Content-Length',str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif '/lechuang-image/' in self.path and '/generations/' in self.path:
            self._json({'data': {
                'status': 'succeeded',
                'output': {'images': [{'url': 'http://127.0.0.1:%d/lechuang-image/file.png' % self.server.port}]},
            }})
        elif self.path.endswith('/file.png'):
            self._png()
        else:
            self._json({'error': 'unknown path'}, 404)


class MockProvider:
    """本地假供应商：/openai-image/* 与 /lechuang-image/* 两套协议各一条。"""

    def __init__(self, port=None):
        # 固定端口：渠道 base_url 会落进数据库并跨进程复用，
        # 随机端口会让「上一次播种的 base_url」在本次运行指向死端口。
        port = int(port if port is not None else os.environ.get('HQ_MOCK_PROVIDER_PORT', '8902'))
        self.httpd = ThreadingHTTPServer(('127.0.0.1', port), _Handler)
        self.httpd.calls = []
        self.httpd.image_size = None
        self.port = self.httpd.server_address[1]
        self.httpd.port = self.port
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def calls(self):
        return self.httpd.calls

    def base_url(self, protocol):
        return 'http://127.0.0.1:%d/%s/v1' % (self.port, protocol)

    def posts(self):
        return [c for c in self.calls if c['method'] == 'POST']

    def reset(self):
        self.calls.clear()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# --------------------------------------------------------------------------- 沙箱 DNS
def _loopback_resolver(host, port, type=0):
    """执行沙箱把未知域名解析到保留网段、并拦截私有地址；这里显式放行回环以指向假供应商。"""
    return [(2, 1, 6, '', ('127.0.0.1', port))]


class IsolatedCase(unittest.TestCase):
    """所有用例的公共隔离环境：临时库 + 临时产物 + 假供应商 + 两条渠道（不同协议）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hq-switch-')
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / 'content_out'
        self.jobs_db = self.tmp.name + '/jobs.db'
        self._make_jobs_db(self.jobs_db)
        self.env = patch.dict(os.environ, {
            'HQ_CHANNEL_DB': self.tmp.name + '/channels.db',
            'HQ_OBSERVABILITY_DB': self.tmp.name + '/trace.db',
            'CONTENT_OUT': str(self.out),
            'CONTENT_JOB_DB': self.jobs_db,
            'HQ_PROVIDER_KEYS_MASTER_KEY': base64.urlsafe_b64encode(os.urandom(32)).decode(),
            'HQ_CHANNEL_STORE': 'sqlite',
        })
        self.env.start()
        self.addCleanup(self.env.stop)

        from server.content_domains import channel_manager as cm
        from server.content_domains import channel_runtime as runtime
        from server.content_domains import channel_parameters as params
        from server.content_domains import core
        from server.content_domains import safe_http
        self.cm, self.runtime, self.params, self.core, self.safe_http = cm, runtime, params, core, safe_http

        # core 的 OUT_DIR 是导入期算好的，跨用例会被上一个临时目录带走；每个用例重新指到自己的产物目录。
        self.out.mkdir(parents=True, exist_ok=True)
        core.OUT_DIR = self.out
        for sub in ('audio', 'video'):
            (self.out / sub).mkdir(parents=True, exist_ok=True)
        core.AUDIO_OUT_DIR = self.out / 'audio'
        core.VIDEO_OUT_DIR = self.out / 'video'

        real_validate = safe_http.validate_target

        def fake_validate(url, proxy=False, resolver=None):
            # 执行沙箱默认拦截回环地址并污染未知域名的解析；这里显式换成固定解析器，
            # 只为把 base_url 指向本地假供应商。
            return real_validate(url, proxy=proxy, resolver=resolver or _loopback_resolver)

        self._resolver = patch.object(safe_http, 'validate_target', side_effect=fake_validate)
        self._resolver.start()
        self.addCleanup(self._resolver.stop)

        self.provider = MockProvider()
        self.addCleanup(self.provider.stop)

        self.operation = 'image.xiaole.text'      # 文生图：不需要参考图、不需要蒙版

    # -- 建渠道 ---------------------------------------------------------------
    @staticmethod
    def _make_jobs_db(path):
        """渠道下线前的「是否还有未结束任务」核对需要任务库；给一个隔离的空库。"""
        with closing(sqlite3.connect(path, timeout=10)) as c:
            c.execute("""CREATE TABLE IF NOT EXISTS jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,username TEXT,cost INTEGER,
                status TEXT DEFAULT 'pending',payload TEXT,result TEXT,error TEXT,
                created_at INTEGER,updated_at INTEGER,owner TEXT,refunded INTEGER DEFAULT 0)""")
            c.commit()

    def make_channel(self, kind, name, model, protocol, secret):
        """kind='A' 走 openai_image 协议，kind='B' 走 lechuang_image 协议。"""
        return self.cm.save('admin', {
            'name': name,
            'adapter': 'openai_image' if kind == 'A' else 'lechuang_image',
            'model': model,
            'base_url': self.provider.base_url('openai-image' if kind == 'A' else 'lechuang-image'),
            'secret': secret,
            'supplier': '假供应商 ' + kind,
            'connection_type': 'relay',
            'enabled': True,
            'fixture': {'prompt': 'fixture'},
            'rpm': 60,
            'daily_limit': 5,
            'test_cost': 1,
            'daily_budget': 20,
        })

    def give_full_test(self, channel_id, state='passed'):
        run = self.cm.reserve(channel_id, 'full')
        self.cm.finish(run, state, 'fixture checked')

    def payload(self, prompt='一只猫'):
        return {'source_page': 'banana', 'provider': 'xiaole', 'prompt': prompt,
                'size': '1024x1024', 'quality': 'high'}

    # -- 便捷断言 -------------------------------------------------------------
    def switch_to(self, channel_id, channels, expected_revision=0, state='managed'):
        return self.cm.save_operation_mapping('admin', {
            'operation_id': self.operation, 'state': state,
            'channels': channels, 'expected_revision': expected_revision,
        })
