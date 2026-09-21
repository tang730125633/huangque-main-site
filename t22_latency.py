# -*- coding: utf-8 -*-
"""隔离验证「检测延迟」：各种 HTTP 状态 / 超时 / 连不上 / 无地址 的返回。"""
import base64, http.server, json, os, socket, sys, tempfile, threading, time
sys.path.insert(0, 'server')
os.environ.update({'HQ_CHANNEL_DB': tempfile.mkdtemp()+'/c.db',
                   'HQ_OBSERVABILITY_DB': tempfile.mkdtemp()+'/t.db',
                   'HQ_PROVIDER_KEYS_MASTER_KEY': base64.urlsafe_b64encode(b'a'*32).decode()})
os.environ.pop('HQ_CHANNEL_STORE', None)

# ── 模拟服务：按路径返回不同状态 ──
CODES = {'/ok':200, '/nf':404, '/auth':401, '/forbid':403, '/nohd':405,
         '/err':500, '/bad':502, '/redir':302, '/slow':None}
class H(http.server.BaseHTTPRequestHandler):
    def do_HEAD(self):
        code = CODES.get(self.path, 200)
        if code is None:                 # 超时：挂住不响应
            time.sleep(15); return
        self.send_response(code)
        if code == 302: self.send_header('Location', 'https://example.com/')
        self.end_headers()
    def do_GET(self): self.do_HEAD()
    def log_message(self, *a): pass

srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
print("模拟服务: http://127.0.0.1:%d  （200/401/403/404/405/500/502/302/超时）" % PORT)

from content_domains import channel_latency, channel_manager as cm, safe_http
# safe_http 会拦私网地址，探测本机模拟服务需要放开
import unittest.mock as M
_real = safe_http.validate_target
def _res(host, port, type=0):
    return [(2, 1, 6, '', ('127.0.0.1', port))]
ctx = M.patch.object(safe_http, 'validate_target',
    side_effect=lambda url, proxy=False, resolver=None: _real(url, proxy=proxy,
        resolver=resolver or _res))
ctx.start()

def mk(path, name):
    return cm.save('admin', dict(name=name, adapter='openai_image', model='x',
        base_url='http://127.0.0.1:%d%s' % (PORT, path), secret='sk-x', enabled=True,
        fixture={'prompt':'x'}, daily_limit=1, test_cost=1, daily_budget=1))

cases = [('/ok','200'),('/nf','404'),('/auth','401'),('/forbid','403'),('/nohd','405'),
         ('/err','500'),('/bad','502'),('/redir','302'),('/slow','超时')]
print("\n%-10s %-9s %-18s %-7s %-8s %s" % ("路径","state","reason","状态码","可达","message"))
for path, label in cases:
    ch = mk(path, 'lat'+label)
    r = channel_latency.measure('admin', {'uid':'managed:'+ch['id']}, lambda: [])
    print("%-10s %-9s %-18s %-7s %-8s %s" % (
        path, r.get('state'), r.get('reason'), r.get('http_status'),
        r.get('network_reachable'), r.get('message') or '(空)'))

# 连不上：指向一个没人监听的端口
ch = mk(':1', 'lat-连不上') if False else cm.save('admin', dict(name='lat-连不上', adapter='openai_image',
        model='x', base_url='http://127.0.0.1:9/dead', secret='sk-x', enabled=True,
        fixture={'prompt':'x'}, daily_limit=1, test_cost=1, daily_budget=1))
r = channel_latency.measure('admin', {'uid':'managed:'+ch['id']}, lambda: [])
print("%-10s %-9s %-18s %-7s %-8s %s" % ('连不上', r.get('state'), r.get('reason'),
      r.get('http_status'), r.get('network_reachable'), r.get('message')))

# 无地址
ch = cm.save('admin', dict(name='lat-无地址', adapter='openai_image', model='x',
    base_url='', secret='sk-x', enabled=True, fixture={'prompt':'x'},
    daily_limit=1, test_cost=1, daily_budget=1))
r = channel_latency.measure('admin', {'uid':'managed:'+ch['id']}, lambda: [])
print("%-10s %-9s %-18s %-7s %-8s %s" % ('无地址', r.get('state'), r.get('reason'),
      r.get('http_status'), r.get('network_reachable'), r.get('message')))

print("\n══ 安全检查：请求不带凭据、不触发生成 ══")
print("  authentication_tested:", r.get('authentication_tested'))
print("  generation_tested    :", r.get('generation_tested'))
print("  measurement          :", r.get('measurement'))
ctx.stop(); srv.shutdown()
