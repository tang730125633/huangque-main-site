#!/usr/bin/env python3
"""后台「直接切换接单渠道」——本地隔离测试服务（端口 8901）。

用途：让渠道管理页面的**写操作**（拖动 / 设为主渠道 / 发布）真的打到这个隔离服务上，
从而可以在浏览器里走完整流程；它**不经过 mirror.py**，也不连生产。

端口分工：
    8899  生产镜像（mirror.py，只读转发到生产）—— 保持不动
    8901  本服务（隔离库 + 真实后端代码，可安全写）
    8902  假供应商（harness.MockProvider，进程内随机端口）

启动：
    python local_service.py
然后打开：http://127.0.0.1:8901/admin-console/#channel
"""

import itertools
import time
import base64
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path(os.environ.get('HQ_REPO') or r'E:\AI\临时\hq-cov')
PORT = int(os.environ.get('HQ_SWITCH_PORT', '8901'))
DATA = Path(os.environ.get('HQ_SWITCH_DATA', str(HERE / '.data')))

# 本地隔离管理员：只为让控制台外壳解锁，权限与生产账号无关。
LOCAL_ADMIN = {'username': 'local-admin', 'name': '本地隔离管理员', 'role': 'admin',
               'points': 0, 'membership': 'internal', 'local_only': True}

# 界面驱动回传的进度（见 _driver_script）。
DRIVE_REPORT = []


def run_task_inline(binding, payload, job_id):
    """在服务进程里跑一次真实任务：假供应商就在本进程，能收到真实 HTTP 调用。"""
    import pathlib
    from server.content_domains import channel_runtime as runtime, core
    out = DATA / 'content_out'
    out.mkdir(parents=True, exist_ok=True)
    core.OUT_DIR = out
    for sub in ('audio', 'video'):
        (out / sub).mkdir(parents=True, exist_ok=True)
    core.AUDIO_OUT_DIR = out / 'audio'
    core.VIDEO_OUT_DIR = out / 'video'
    return runtime.run_task(binding, payload, job_id)

sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'server'))

# ── 隔离环境：必须在导入被测模块之前设置 ──────────────────────────────────────
DATA.mkdir(parents=True, exist_ok=True)
(DATA / 'content_out').mkdir(exist_ok=True)
os.environ.setdefault('HQ_CHANNEL_DB', str(DATA / 'channels.db'))
os.environ.setdefault('HQ_OBSERVABILITY_DB', str(DATA / 'trace.db'))
os.environ.setdefault('CONTENT_OUT', str(DATA / 'content_out'))
os.environ.setdefault('CONTENT_JOB_DB', str(DATA / 'jobs.db'))
os.environ.setdefault('HQ_CHANNEL_STORE', 'sqlite')
os.environ.setdefault('HQ_PROVIDER_KEYS_MASTER_KEY',
                      base64.urlsafe_b64encode(b'local-test-master-key-32bytes!!!').decode())
os.environ.pop('HQ_DATABASE_URL', None)      # 绝不允许走到生产 PG

# ── 受控替身：auth-service ───────────────────────────────────────────────
# 生产处理器（content_api.H）会去 AUTH_BASE 做登录态校验、扣点与退点。
# 这里把它指向本进程内的替身：鉴权、扣费是「受控替身」，其余全是真实代码。
import stub_auth                                                   # noqa: E402
_STUB_AUTH, _STUB_AUTH_PORT = stub_auth.start()
os.environ['AUTH_BASE'] = 'http://127.0.0.1:%d' % _STUB_AUTH_PORT
os.environ.setdefault('HQ_INTERNAL_TOKEN', 'local-internal-token')
os.environ['MAX_USER_ACTIVE_JOBS'] = '20'


from harness import MockProvider, _loopback_resolver   # noqa: E402

from server.content_domains import channel_manager as cm             # noqa: E402
from server.content_domains import channel_parameters as cp          # noqa: E402
from server.content_domains import feature_flags as ff               # noqa: E402
from server.content_domains import frontend_channel_matrix as fcm    # noqa: E402
from server.content_domains import safe_http                         # noqa: E402
# 复用【生产请求处理器】：真实的字段清洗、报价、版本校验、任务入库与渠道绑定。
# 只有鉴权与扣费走上面的受控替身，供应商走本地假供应商。
# /api/gen/banana 的生产处理器就是 imggen_api.H —— nginx 把该路径路由到 8101，
# 由 imggen 服务处理；content_api.H（8096，域注册表分发）反而会把它 404 掉，
from imggen_api import H as ProductionHandler                      # noqa: E402
import imggen_api                                                  # noqa: E402


# 沙箱 DNS：只为了让假供应商的 127.0.0.1 能通过地址校验
_real_validate = safe_http.validate_target
safe_http.validate_target = (
    lambda url, proxy=False, resolver=None:
    _real_validate(url, proxy=proxy, resolver=resolver or _loopback_resolver))

PROVIDER = MockProvider()

# ── 真实 HTTP 受理入口的任务登记 ──────────────────────────────────────
# 走的是与生产同一条通道：channel_manager.capture 负责识别功能、校验参数
# 版本与报价、绑定渠道快照；随后在服务进程里执行（假供应商收到真实 HTTP 调用）。
JOBS = {}
JOB_SEQ = itertools.count(1)


def http_submit(kind, body):
    """受理一次生成请求，返回任务记录。不扣费（隔离环境），其余与生产同路。"""
    captured = cm.capture(kind, body)
    binding = captured.get('_channel_binding') or {}
    # 观测库是持久的，而 JOB_SEQ 每个进程从 1 重新开始，直接用会撞上
    # 「该任务已有渠道执行记录」。用时间戳打底，保证跨进程唯一。
    job_id = int(time.time() * 1000) % 10 ** 9 + next(JOB_SEQ)
    PROVIDER.reset()
    job = {'id': job_id, 'kind': kind, 'status': 'running', 'phase': '已受理',
           'channel_id': binding.get('id'), 'channel_version': binding.get('version'),
           'operation_id': binding.get('operation_id'), 'model': binding.get('model'),
           'provider_calls': [], 'result': None, 'error': None}
    JOBS[job_id] = job
    try:
        result = run_task_inline(binding, captured, job_id)
        job['status'] = 'done'
        job['phase'] = '成品已返回'
        job['result'] = result
    except Exception as exc:                                  # noqa: BLE001
        job['status'] = 'failed'
        job['error'] = '%s: %s' % (type(exc).__name__, exc)
    job['provider_calls'] = list(PROVIDER.calls)
    return job


STATIC = REPO / 'site' / 'admin'
STATIC_WORKBENCH = REPO / 'site'


def repoint_channels():
    """把已有渠道的 base_url 重新指向本次进程里的假供应商。

    渠道的 base_url 是播种时写进去的；隔离库是持久的，而假供应商每次启动
    端口不同，不重指就会出现「地址是上一次那个进程的」这种假失败。
    """
    kinds = {'openai_image': 'openai-image', 'lechuang_image': 'lechuang-image',
             'sora_video': 'sora-a', 'lechuang_video': 'lechuang-video'}
    for item in cm.overview().get('items') or []:
        kind = kinds.get(item.get('adapter'))
        if not kind:
            continue
        want = PROVIDER.base_url(kind)
        if item.get('base_url') == want:
            continue
        body = dict(item)
        body['base_url'] = want
        body['version'] = item['version']
        body['secret'] = 'keep'
        try:
            cm.save('seed', body)
        except Exception:
            pass


def publish_parameters(cid, points):
    """给隔离渠道发布一份参数契约。

    两条渠道故意用不同点数，这样「报价跟着有效渠道走」是可证的，
    而不是看起来对。
    """
    cap = cp.capabilities(cm.version(cid))
    values = {k: v[0] for k, v in cap['fields'].items()}
    spec = dict(profile=cap['profile'],
                fields=[dict(key=k, label=cp.LABELS.get(k, k), visible=True) for k in cap['fields']],
                combinations=[dict(id='std', values=values, points=points)], default='std',
                reference_min=cap['reference_min'], reference_max=cap['reference_max'])
    for action in ('draft', 'publish'):
        state = cp.admin_state(cid)
        cp.change('local-admin', dict(id=cid, version=state['version'],
                                      draft_revision=(state['draft'] or {}).get('revision', 0),
                                      action=action, parameters=spec, confirmed=True))


def _make_jobs_db():
    import sqlite3
    path = os.environ['CONTENT_JOB_DB']
    if not Path(path).exists():
        with sqlite3.connect(path) as c:
            c.execute("""CREATE TABLE IF NOT EXISTS jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,username TEXT,cost INTEGER,
                status TEXT DEFAULT 'pending',payload TEXT,result TEXT,error TEXT,
                created_at INTEGER,updated_at INTEGER,owner TEXT,refunded INTEGER DEFAULT 0)""")


def seed():
    """两条能力相同、协议/模型/参数都不同的渠道，以及一条待切的功能映射。"""
    # jobs 库必须只有【一个】。
    #
    # 生产上，core.jdb() 用的是模块级 core.JOB_DB（= content-api/content_jobs.db），
    # 而 imggen_api.jdb() 用的是它自己的 JOB_DB（默认同名文件）。两边之所以一致，
    # 是因为生产没有设置 CONTENT_JOB_DB，两者都回落到 content-api 目录。
    # 隔离环境一旦把 CONTENT_JOB_DB 指到别处，建表与受理就会分裂成两个库
    # （症状：no such table: jobs）。
    #
    # 因此这里显式把两处都对齐到同一个隔离文件，而不是各自回落。
    from server.content_domains import core as _core
    import imggen_api as _imggen
    _job_db = str(DATA / 'content_jobs.db')
    _core.JOB_DB = _job_db
    _imggen.JOB_DB = _job_db
    _core.init_db()                     # 用生产的建表逻辑，不用自写 schema
    if cm.overview()['items']:
        return
    a = cm.save('seed', {
        'name': '甲 · 乐创生图', 'adapter': 'openai_image', 'model': 'gpt-image-2',
        'base_url': PROVIDER.base_url('openai-image'), 'secret': 'secret-of-A-aaaa',
        'supplier': '假供应商 甲', 'connection_type': 'relay', 'enabled': True,
        'fixture': {'prompt': 'fixture'}, 'rpm': 60, 'daily_limit': 5,
        'test_cost': 1, 'daily_budget': 20})
    b = cm.save('seed', {
        'name': '乙 · 原生生图', 'adapter': 'lechuang_image', 'model': 'seedream-5.0-pro',
        'base_url': PROVIDER.base_url('lechuang-image'), 'secret': 'secret-of-B-bbbb',
        'supplier': '假供应商 乙', 'connection_type': 'relay', 'enabled': True,
        'fixture': {'prompt': 'fixture'}, 'rpm': 60, 'daily_limit': 5,
        'test_cost': 1, 'daily_budget': 20})
    cm.save_operation_mapping('seed', {
        'operation_id': 'image.xiaole.text', 'state': 'managed',
        'channels': [a['id']], 'expected_revision': 0})
    # Sora 两条：协议与模型都不同，用来验收「拖到第一位 → 新任务走目标渠道」。
    sora_a = cm.save('seed', {
        'name': 'Sora 原线路 A', 'adapter': 'sora_video', 'model': 'sora-2',
        'base_url': PROVIDER.base_url('sora-a'), 'secret': 'sk-ORIGINAL-A',
        'supplier': 'OpenAI', 'connection_type': 'official', 'enabled': True,
        'fixture': {'prompt': 'fixture'}, 'rpm': 60, 'daily_limit': 5,
        'test_cost': 1, 'daily_budget': 20})
    sora_b = cm.save('seed', {
        'name': 'Sora 新渠道 B', 'adapter': 'sora_video', 'model': 'sora-2-pro',
        'base_url': PROVIDER.base_url('sora-b'), 'secret': 'sk-CHANNEL-B',
        'supplier': '中转 API', 'connection_type': 'relay', 'enabled': True,
        'fixture': {'prompt': 'fixture'}, 'rpm': 60, 'daily_limit': 5,
        'test_cost': 1, 'daily_budget': 20})
    cm.save_operation_mapping('seed', {
        'operation_id': 'video.sora.text', 'state': 'managed',
        'channels': [sora_a['id']], 'expected_revision': 0})
    # 两条渠道发布【不同点数】的参数契约：报价跟着有效渠道走是可证的，
    # 而不是看起来对。
    publish_parameters(a['id'], 18)   # 甲：18 点
    publish_parameters(b['id'], 35)   # 乙：35 点
    print('  已播种：甲=%s 乙=%s | SoraA=%s SoraB=%s' % (
        a['id'], b['id'], sora_a['id'], sora_b['id']))


def overview_payload():
    result = cm.overview()
    result['frontend_matrix'] = fcm.build(
        result, [], {}, cp.admin_layout_state(), ff.list_features())
    return result


def _driver_script():
    """注入到控制台页面里的界面驱动。

    控制台在 load 之后会用 document.write 重建整个文档，注入到 DOM 里的节点和 title 都会被冲掉，
    所以每一步结果都 **回传服务端**（/__drive-report），而不是写在 DOM 里。
    """
    return """<script>
(function () {
  const report = o => fetch('/__drive-report', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(o)}).catch(() => {});
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const until = async (fn, ms) => { const t = Date.now();
    while (Date.now() - t < ms) { if (fn()) return true; await wait(150); } return false; };
  async function run() {
    const log = [];
    const step = tag => { log.push(tag); report({step: tag}); };
    if (sessionStorage.getItem('hqDriveSoraReload')) {
      sessionStorage.removeItem('hqDriveSoraReload');
      await wait(1500);
      const videoTab = document.querySelector('[data-cm-matrix-page="video"]');
      if (videoTab) { videoTab.click(); await wait(700); }
      const soraModel = [...document.querySelectorAll('[data-cm-model-key]')].find(b => /sora/i.test(b.textContent || ''));
      if (soraModel) {
        soraModel.click(); await wait(1200);
        const rows = [...document.querySelectorAll('.cm-priority-channel[data-cm-priority-channel]')].map(n => n.querySelector('.cm-priority-info strong')?.textContent.trim());
        step('sora-after-reload-rows=' + JSON.stringify(rows));
        const orig = [...document.querySelectorAll('.cm-priority-channel')].some(n => /原线路|原厂/.test(n.textContent || ''));
        step('sora-after-reload-original-kept=' + orig);
      }
      report({done: true, log});
      return;
    }
    try {
      step('loaded');
      const unlocked = await until(() => !document.body.classList.contains('admin-locked'), 15000);
      step('unlocked=' + unlocked);
      if (!unlocked) { report({done: true, log}); return; }
      const nav = document.querySelector('[data-module-tab="managedChannels"]');
      step('nav=' + !!nav);
      if (nav) nav.click();
      await wait(1200);
      // ★ Sora 专项：切到视频栏目 -> Sora 模型 -> 拖动
      step('sora-begin');
      // 分类抽屉是 details，必须先展开再点「生视频」
      const picker = document.querySelector('details.cm-category-picker');
      if (picker && !picker.open) { picker.querySelector('summary')?.click(); await wait(300); }
      const videoTab = document.querySelector('[data-cm-matrix-page="video"]');
      if (videoTab) { videoTab.click(); await wait(1200); }
      step('video-tab=' + !!videoTab);
      const soraModel = [...document.querySelectorAll('[data-cm-model-key]')].find(b => /sora/i.test(b.textContent || ''));
      step('sora-model=' + !!soraModel);
      if (soraModel) {
        soraModel.click();
        await wait(800);
        const rowsBefore = [...document.querySelectorAll('.cm-priority-channel[data-cm-priority-channel]')].map(n => n.querySelector('.cm-priority-info strong')?.textContent.trim());
        step('sora-rows-before=' + JSON.stringify(rowsBefore));
        const anchor = [...document.querySelectorAll('.cm-priority-channel')].find(n => /原厂|原线路|Sora 原线路/.test(n.textContent || ''));
        step('sora-original-row=' + !!anchor);
        const list = [...document.querySelectorAll('.cm-priority-channel[data-cm-priority-channel], .cm-priority-channel[data-cm-priority-anchor]')];
        if (list.length >= 2) {
          const dragRow = [...document.querySelectorAll('.cm-priority-channel[data-cm-priority-channel]')].find(n => /新渠道 B/.test(n.textContent || '')) || list[1];
          const handle = dragRow.querySelector('.cm-priority-drag');
          const r0 = list[0].getBoundingClientRect(), r1 = dragRow.getBoundingClientRect();
          if (handle && r1.top > r0.top) {
            const fire = (t, y) => handle.dispatchEvent(new PointerEvent(t, {bubbles:true,cancelable:true,pointerId:9,button:0,buttons:t==='pointerup'?0:1,clientX:r1.left+8,clientY:y}));
            fire('pointerdown', r1.top + 8); fire('pointermove', r0.top + 2); fire('pointermove', r0.top + 2); fire('pointerup', r0.top + 2);
            await wait(5000);
            step('sora-rows-after=' + JSON.stringify([...document.querySelectorAll('.cm-priority-channel[data-cm-priority-channel]')].map(n => n.querySelector('.cm-priority-info strong')?.textContent.trim())));
            const s = document.querySelector('[data-cm-priority-status]');
            step('sora-status=' + ((s && s.textContent) || '').trim().slice(0,80));
          }
        }
        // 刷新页面验证顺序保留
        await wait(500);
        step('sora-reloading');
        sessionStorage.setItem('hqDriveSoraReload','1');
        location.reload();
        return;
      }
      const found = await until(() => document.querySelector('[data-cm-priority-first]'), 20000);
      step('switch-button=' + found);
      if (!found) { step('no-button'); report({done: true, log}); return; }
      const btn = document.querySelector('[data-cm-priority-first]');
      step('target=' + (btn.dataset.cmPriorityFirst || ''));
      step('label=' + (btn.textContent || '').trim());
      const firstName = (() => {
        const row = document.querySelector('.cm-priority-channel[data-cm-priority-channel] .cm-priority-info strong');
        return row ? row.textContent.trim() : '';
      })();
      step('before-first=' + firstName);
      btn.click();
      await until(() => {
        const s = document.querySelector('[data-cm-priority-status]');
        return s && /已切换|待确认|未应用/.test(s.textContent || '');
      }, 20000) || await wait(4000);
      const status = document.querySelector('[data-cm-priority-status]');
      step('status=' + ((status && status.textContent) || '').trim().slice(0, 140));
      const rows = [...document.querySelectorAll('.cm-priority-channel[data-cm-priority-channel]')]
        .map(n => n.querySelector('.cm-priority-info strong')?.textContent.trim());
      step('rows=' + JSON.stringify(rows));

      // 界面刚把目标渠道设为主渠道，立刻用当前映射跑一个真实任务，看它到底打哪个渠道。
      const statusNode = document.querySelector('[data-cm-priority-status]');
      const opId = statusNode && statusNode.getAttribute('data-cm-priority-status');
      step('op=' + opId);
      if (opId) {
        await wait(600);
        const r = await fetch('/__run-task', {method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({job_id: 2001, operation_id: opId})}).then(x => x.json()).catch(e => ({err: e.message}));
        const post = (r && r.provider_post) || {};
        step('task-path=' + (post.path || '(无)'));
        step('task-auth=' + (post.authorization || '(无)'));
        step('task-model=' + ((post.body || {}).model || '(无)'));
        step('task-posts=' + (r && r.post_count));
        step('task-candidates=' + JSON.stringify(r && r.route_candidates));
        step('task-skipped=' + JSON.stringify(r && r.route_skipped));
      }

      // ── 第二步：真正的拖动（指针事件），把当前第二行拖到第一位 ──
      const list = [...document.querySelectorAll('.cm-priority-channel[data-cm-priority-channel], .cm-priority-channel[data-cm-priority-anchor]')];
      step('drag-rows=' + list.length);
      if (list.length >= 2) {
        const handle = list[1].querySelector('.cm-priority-drag');
        const r0 = list[0].getBoundingClientRect();
        const r1 = list[1].getBoundingClientRect();
        step('drag-handle=' + !!handle + ' rects=' + JSON.stringify([Math.round(r0.top), Math.round(r1.top)]));
        if (handle && r1.top > r0.top) {
          const fire = (type, y) => handle.dispatchEvent(new PointerEvent(type, {
            bubbles: true, cancelable: true, pointerId: 7, button: 0,
            buttons: type === 'pointerup' ? 0 : 1, clientX: r1.left + 8, clientY: y,
          }));
          fire('pointerdown', r1.top + 8);
          fire('pointermove', r0.top + 2);
          fire('pointermove', r0.top + 2);
          fire('pointerup', r0.top + 2);
          await until(() => {
            const s = document.querySelector('[data-cm-priority-status]');
            return s && /已切换|待确认|未应用/.test(s.textContent || '');
          }, 20000) || await wait(4000);
          const after = document.querySelector('[data-cm-priority-status]');
          step('drag-status=' + ((after && after.textContent) || '').trim().slice(0, 140));
          const order = [...document.querySelectorAll('.cm-priority-channel[data-cm-priority-channel]')]
            .map(n => n.querySelector('.cm-priority-info strong')?.textContent.trim());
          step('drag-rows-after=' + JSON.stringify(order));
        }
      }
    } catch (e) { step('error=' + e.message); }
    report({done: true, log});
  }
  if (document.readyState === 'complete') setTimeout(run, 800);
  else window.addEventListener('load', () => setTimeout(run, 800));
})();
</script>"""


class Handler(ProductionHandler):
    """隔离处理器 = 生产处理器 + 少量本地路由。

    /api/gen/banana、/api/gen/image 等生成入口**不重写**，直接走生产代码。
    只补：本地管理接口、假供应商证据查询、驱动脚本注入。
    """

    protocol_version = 'HTTP/1.1'
    server_version = 'hq-switch-local'

    def log_message(self, fmt, *args):
        sys.stderr.write('  %s\n' % (fmt % args))

    # -- helpers --------------------------------------------------------------
    def _send(self, code, body, content_type='application/json', extra=None):
        # str 直转 UTF-8：走 json.dumps 会把整个 HTML 当 JSON 编码（加引号 + 转义），
        # 浏览器拿到的是转义字符串而不是可解析的页面，注入的脚本永远不执行。
        if isinstance(body, bytes):
            data = body
        elif isinstance(body, str):
            data = body.encode('utf-8')
        else:
            data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self):
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b''
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return None

    # -- 静态页面 -------------------------------------------------------------
    def do_GET(self):
        path = self.path.split('?')[0]
        # 控制台外壳的登录门调的是 /api/auth/*（不是 /api/admin/*）。
        # 隔离环境里直接给一个本地管理员身份，不读也不写生产登录态。
        if path == '/api/auth/me':
            return self._send(200, {'user': dict(LOCAL_ADMIN)})
        if path == '/__provider-calls':
            return self._send(200, {'calls': PROVIDER.calls, 'provider_port': PROVIDER.port})
        if path.startswith('/api/auth/'):
            return self._send(200, {'ok': True})
        if path.startswith('/api/admin/channel-manager'):
            try:
                return self._send(200, overview_payload())
            except Exception as exc:                       # noqa: BLE001
                return self._send(500, {'detail': '隔离服务读取失败：%s' % exc})
        if path == '/api/gen/channel-parameters':
            # 原生页面读的就是这个：功能目录 + 该功能当前有效渠道的参数与点数
            try:
                items = cp._published_items()
            except Exception as exc:                       # noqa: BLE001
                # 目录读不到要如实报错，不能让页面退回硬编码价格继续提交
                return self._send(503, {'detail': '参数目录读取失败：%s' % exc})
            return self._send(200, {'items': items, 'refresh_seconds': 30})
        if path.startswith('/api/gen/'):
            return ProductionHandler.do_GET(self)
        if path == '/__ledger':
            return self._send(200, {'ledger': list(stub_auth.LEDGER), 'user': dict(stub_auth.USER)})
        if path.startswith('/api/admin/'):
            return self._send(200, {})                     # 其他模块留空，够控制台启动
        if path in ('/', '/admin-console', '/admin-console/'):
            return self._file(STATIC / 'index.html')
        if path == '/__drive':
            html = (STATIC / 'index.html').read_text(encoding='utf-8')
            html = html.replace('</body>', _driver_script() + '</body>')
            return self._send(200, html, 'text/html; charset=utf-8')
        if path.startswith('/admin/'):
            return self._file(STATIC / path[len('/admin/'):])
        return self._file(STATIC_WORKBENCH / path.lstrip('/'))

    def do_POST(self):
        path = self.path.split('?')[0]
        if path == '/__run-task':
            # 用当前（可能刚被界面改过的）映射跑一个真实任务；证据是假供应商收到的 HTTP 调用。
            # 注意：capture 是 **按请求体自己分类** operation 的（不信任客户端传的 id），
            # 所以载荷要按目标功能的 task_match 构造，否则会被分类到另一个功能。
            body = self._json_body() or {}
            operation_id = str(body.get('operation_id') or 'image.openai.text')
            from server.content_domains import function_registry as registry
            match = dict((registry.operation(operation_id) or {}).get('task_match') or {})
            # kind 必须先取出来再剔除：它决定 capture 按哪个任务类型分类，
            # 放在 pop 之后读会永远退回 'image'，Sora 载荷会被当生图处理。
            kind = str(match.get('kind') or 'image')
            match.pop('kind', None)
            match.pop('reference_count', None)
            match.pop('mask_present', None)
            payload = dict(match, prompt=str(body.get('prompt') or '本地隔离验证'),
                           size='1024x1024', quality='high')
            PROVIDER.reset()
            try:
                captured = cm.capture(kind, payload)
                binding = captured.get('_channel_binding') or {}
                result = run_task_inline(binding, captured, int(body.get('job_id') or 1))
            except Exception as exc:                            # noqa: BLE001
                return self._send(400, {'ok': False, 'want_operation': operation_id,
                                        'db': os.environ.get('HQ_CHANNEL_DB'),
                                        'db_items': [i['id'] for i in cm.overview()['items']],
                                        'detail': '%s: %s' % (type(exc).__name__, exc),
                                        'calls': PROVIDER.calls})
            posts = [c for c in PROVIDER.calls if c['method'] == 'POST']
            return self._send(200, {
                'ok': True,
                'want_operation': operation_id,
                'got_operation': binding.get('operation_id'),
                'channel_id': binding.get('id'),
                'model': binding.get('model'),
                'route_order': binding.get('route_order'),
                'route_candidates': [c.get('id') for c in binding.get('route_candidates') or []],
                'route_skipped': binding.get('route_skipped'),
                'provider_post': posts[0] if posts else None,
                'post_count': len(posts),
                'delivered': {k: (result or {}).get(k) for k in ('channel_id', 'model', 'mode')},
            })
        if path == '/__start-workers':
            imggen_api.start_job_workers()
            return self._send(200, {'ok': True, 'started': True})
        if path == '/__drive-report':
            body = self._json_body() or {}
            DRIVE_REPORT.append(body)
            with open(DATA / 'drive-report.jsonl', 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(body, ensure_ascii=False) + '\n')
            step = body.get('step')
            if step:
                print('  [drive] %s' % step)
            return self._send(200, {'ok': True})
        if path == '/api/auth/login':
            return self._send(200, {'ok': True, 'user': dict(LOCAL_ADMIN)})
        if path == '/api/auth/logout':
            return self._send(200, {'ok': True})
        if path.startswith('/api/gen/'):
            # 生成入口一律交回生产处理器：不在隔离服务里重写受理逻辑
            return ProductionHandler.do_POST(self)
        body = self._json_body()
        if body is None:
            return self._send(400, {'detail': '请求体不是合法 JSON'})
        if path == '/api/admin/channel-manager/operation-mapping':
            try:
                saved = cm.save_operation_mapping('local-admin', body)
            except ValueError as exc:
                return self._send(400, {'ok': False, 'detail': str(exc)})
            except Exception as exc:                       # noqa: BLE001
                return self._send(500, {'ok': False, 'detail': '切换失败：%s' % exc})
            return self._send(200, {'ok': True, 'mapping': saved})
        if path.startswith('/api/admin/channel-manager/'):
            return self._send(400, {'ok': False, 'detail': '本地隔离服务只实现了 operation-mapping'})
        return self._send(404, {'detail': '隔离服务没有这个写接口'})

    def _inject_drive(self, target, data):
        """给工作台页面注入驱动脚本：真实浏览器里完成选择→填写→提交。

        只在隔离服务里注入；生产页面不受影响。
        """
        if os.environ.get('HQ_WORKBENCH_DRIVE') != '1':
            return data
        if not str(target).endswith(('.html',)):
            return data
        end = data.lower().rfind(b'</body>')
        if end < 0:
            return data
        script = (b"<script>(function(){var q=new URLSearchParams(location.search);"
                  b"if(!q.get('hqdrive'))return;window.__hqdrive=[];"
                  b"function log(s){window.__hqdrive.push(s);try{fetch('/__drive-report',{method:'POST',"
                  b"headers:{'Content-Type':'application/json'},body:JSON.stringify({step:s})})}catch(e){}}"
                  b"window.__hqlog=log;"
                  b"</script>")
        return data[:end] + script + data[end:]

    def _file(self, target):
        self._inject_target = target
        try:
            target = Path(target).resolve()
            if not str(target).startswith(str(REPO.resolve())):
                return self._send(403, {'detail': '越界'})
            data = target.read_bytes()
        except (OSError, ValueError):
            return self._send(404, {'detail': 'not found'})
        kind = {'.html': 'text/html; charset=utf-8', '.js': 'application/javascript; charset=utf-8',
                '.css': 'text/css; charset=utf-8', '.png': 'image/png', '.svg': 'image/svg+xml',
                '.json': 'application/json; charset=utf-8'}.get(target.suffix, 'application/octet-stream')
        return self._send(200, self._inject_drive(target, data), kind)


def main():
    # 生产处理器把任务投进队列，由 worker 池消费；隔离服务必须同样启动它，
    # 否则任务永远停在排队态，看不到真实的渠道执行。
    #
    # HQ_WORKER_PAUSED=1 时不启动：用来验收「先创建待执行任务 → 切渠道 →
    # 再放行执行」这个场景。这是【隔离测试设施】的开关，生产没有这个功能，
    # 也不需要为测试新增暂停能力。
    if os.environ.get('HQ_WORKER_PAUSED') == '1':
        print('  [isolated] worker 已暂停（HQ_WORKER_PAUSED=1），等待 /__start-workers', flush=True)
    else:
        imggen_api.start_job_workers()
    seed()
    repoint_channels()
    httpd = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    print('=' * 68)
    print(' 黄雀「后台直接切换接单渠道」本地隔离测试服务')
    print('=' * 68)
    print('  页面   http://127.0.0.1:%d/admin-console/' % PORT)
    print('  渠道库 %s   (隔离 SQLite，非生产)' % os.environ['HQ_CHANNEL_DB'])
    print('  假供应商 http://127.0.0.1:%d/  (记录每次调用)' % PROVIDER.port)
    print('  保存/切换会真的写这个隔离库；不连生产、不经过 mirror.py')
    print('  Ctrl+C 停止')
    print('=' * 68)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        PROVIDER.stop()


if __name__ == '__main__':
    main()
