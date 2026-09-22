# -*- coding: utf-8 -*-
"""受控替身：auth-service（鉴权 + 扣费 + 退款）。

只做隔离测试用。生产处理器调用 auth-service 的三个端点在这里被接住：
  GET  /api/auth/me              登录态
  POST /api/auth/points/deduct   扣点
  POST /api/auth/points/refund   退点

替身把每一次扣费/退款都记下来，供验收比对「页面显示点数、服务端计算点数、
模拟扣费金额、任务记录」四者是否一致。不产生任何真实费用。
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USER = {'username': 'local-user', 'role': 'admin', 'points': 10 ** 6,
        'points_billing_enabled': True, 'membership_tier': 'initiator'}
LEDGER = []          # [{'kind':'deduct'|'refund','username','amount','reason','transaction_key'}]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/api/auth/me':
            return self._json(200, {'user': dict(USER)})
        if path == '/__ledger':
            return self._json(200, {'ledger': list(LEDGER), 'user': dict(USER)})
        return self._json(404, {'detail': '没有这个接口'})

    def do_POST(self):
        path = self.path.split('?')[0]
        length = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(length) or b'{}')
        except Exception:
            body = {}
        if path in ('/api/auth/points/deduct', '/api/auth/points/refund'):
            kind = 'deduct' if path.endswith('deduct') else 'refund'
            amount = int(body.get('amount') or 0)
            entry = {'kind': kind, 'username': body.get('username'), 'amount': amount,
                     'reason': body.get('reason'), 'transaction_key': body.get('transaction_key')}
            LEDGER.append(entry)
            if kind == 'deduct':
                USER['points'] = int(USER.get('points') or 0) - amount
            else:
                USER['points'] = int(USER.get('points') or 0) + amount
            return self._json(200, {'ok': True, 'points': USER['points'], 'entry': entry})
        if path == '/api/auth/login':
            return self._json(200, {'ok': True, 'user': dict(USER)})
        if path.startswith('/api/auth/'):
            return self._json(200, {'ok': True})
        return self._json(404, {'detail': '没有这个写接口'})


def start(port=None):
    port = int(port or os.environ.get('HQ_STUB_AUTH_PORT') or 0)
    httpd = ThreadingHTTPServer(('127.0.0.1', port), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


if __name__ == '__main__':
    httpd, port = start(os.environ.get('HQ_STUB_AUTH_PORT'))
    print('stub auth on %d' % port, flush=True)
    threading.Event().wait()
