# -*- coding: utf-8 -*-
"""浏览器 A/B 闭环：后台切换 → 真实页面提交 → 目标渠道收到请求 → 成品 → 回切。

判定成功的依据（不看页面静态文字）：
  响应含 job_id → GET /api/gen/job/<id> 为 done → 供应商实际收到 → 目标渠道正确
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(r'E:\AI\临时\hq-cov')
ISO = REPO / 'tests' / 'isolated'
CHROME = r'C:\Users\23329\AppData\Local\Google\Chrome\Application\chrome.exe'
DATA = Path(r'E:\AI\缓存\Temp\abtest')
FUNC = 'image.banana.nb2.text'


def free_port():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); p = s.getsockname()[1]; s.close(); return p


class Client:
    def __init__(self, base):
        self.base = base

    def req(self, path, body=None, token=False):
        data = json.dumps(body).encode() if body is not None else None
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer local-token'
        r = urllib.request.Request(self.base + path, data=data, headers=headers,
                                   method='POST' if body is not None else 'GET')
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b'{}')
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b'{}')

    def revision(self):
        for m in (self.req('/api/admin/channel-manager')[1].get('operation_mappings') or []):
            if m['operation_id'] == FUNC:
                return m['revision']
        return 0

    def switch(self, cid):
        return self.req('/api/admin/channel-manager/operation-mapping',
                        {'operation_id': FUNC, 'state': 'managed',
                         'channels': [cid], 'expected_revision': self.revision()})

    def jobs(self):
        import sqlite3
        con = sqlite3.connect(str(DATA / 'content_jobs.db')); con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute('SELECT id,kind,status,error FROM jobs ORDER BY id')]
        con.close(); return rows


def main():
    if DATA.exists():
        import shutil; shutil.rmtree(DATA)
    port, pport = free_port(), free_port()
    env = dict(os.environ)
    env.update({'HQ_REPO': str(REPO), 'HQ_SWITCH_PORT': str(port),
                'HQ_MOCK_PROVIDER_PORT': str(pport), 'HQ_SWITCH_DATA': str(DATA),
                'HQ_WORKBENCH_DRIVE': '1'})
    proc = subprocess.Popen([sys.executable, str(ISO / 'local_service.py')], cwd=str(ISO), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = 'http://127.0.0.1:%d' % port
    cli = Client(base)
    for _ in range(60):
        try:
            urllib.request.urlopen(base + '/api/auth/me', timeout=2).read(); break
        except Exception:
            if proc.poll() is not None:
                print(proc.stdout.read().decode('utf-8', 'replace')[-1200:]); return
            time.sleep(0.5)

    ov = cli.req('/api/admin/channel-manager')[1]
    A = [i for i in ov['items'] if i['model'] == 'gpt-image-2'][0]
    B = [i for i in ov['items'] if i['model'] == 'seedream-5.0-pro'][0]
    print('  甲=%s(%s)  乙=%s(%s)' % (A['name'], A['id'][:10], B['name'], B['id'][:10]))

    report = DATA / 'drive-report.jsonl'
    for label, ch in (('甲', A), ('乙', B)):
        st, body = cli.switch(ch['id'])
        rev = cli.revision()
        print('\n  ── 后台切到 %s（mapping revision=%s，HTTP %s）──' % (label, rev, st))
        before = len(report.read_text(encoding='utf-8').splitlines()) if report.exists() else 0
        prof = Path(r'E:\AI\缓存\Temp\cab_%s' % label)
        subprocess.run([CHROME, '--headless=new', '--disable-gpu', '--no-sandbox',
                        '--user-data-dir=' + str(prof), '--window-size=1500,1100', '--hide-scrollbars',
                        '--screenshot=' + str(REPO.parent / 'huangque-admin-local' / 'accept' / ('ab_%s.png' % label)),
                        '--virtual-time-budget=40000',
                        base + '/workbench/banana.html?hqdrive=%E7%BA%B3%E7%B1%B3%E9%A6%99%E8%95%89%202&hqprompt=%E4%B8%80%E5%8F%AA%E7%8C%AB'],
                       capture_output=True)
        import shutil; shutil.rmtree(prof, ignore_errors=True)
        time.sleep(4)
        lines = report.read_text(encoding='utf-8').splitlines()[before:] if report.exists() else []
        job_id = None
        for line in lines:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            step = rec.get('step', '')
            print('     %s' % step[:220])
            if step.startswith('RESP') and '"job_id"' in step:
                try:
                    body_json = json.loads(step.split(' body=', 1)[1])
                    job_id = body_json.get('job_id')
                except Exception:
                    pass
        row = {}
        if job_id:
            st, row = cli.req('/api/gen/job/%s' % job_id, token=True)
            print('     GET /api/gen/job/%s → HTTP %s status=%s' % (job_id, st, (row or {}).get('status')))
        posts = [c for c in cli.req('/__provider-calls')[1]['calls'] if c['method'] == 'POST']
        if posts:
            c = posts[-1]
            print('     供应商收到: %s  %s  model=%s' % (c['path'], (c.get('authorization') or '')[:24],
                                                    (c.get('body') or {}).get('model')))
        print('     渠道绑定  : %s' % (str((row or {}).get('channel_id') or '—')[:12]))

    print('\n  ── jobs 表 ──')
    for r in cli.jobs():
        print('     job %s %-6s %-8s %s' % (r['id'], r['kind'], r['status'], str(r['error'] or '')[:50]))
    print('\n  ── 回切到甲 ──')
    st, body = cli.switch(A['id'])
    print('     HTTP %s  revision=%s' % (st, cli.revision()))
    proc.terminate()


if __name__ == '__main__':
    main()
