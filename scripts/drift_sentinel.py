#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黄雀主站「漂移哨兵」——检测有人绕过 ship 直接热改服务器文件。

原理：把"当前线上文件"的 md5（剔除 CRLF）存成基线；日后每天重算并比对。
  - 内容变了 / 文件被删 / 冒出基线里没有的新文件  → 判定为漂移 → 飞书告警
合法部署（走 ship）后应调 `--bless` 刷新基线，这样只有"绕过 ship 的热改"才会报警。

用法：
  drift_sentinel.py            巡检(cron 用)，有漂移则飞书告警(带冷却)
  drift_sentinel.py --bless    以当前线上为准重建基线（部署后/首次执行）
  drift_sentinel.py --test     发一条飞书自检消息
  drift_sentinel.py --print    只打印漂移，不告警（人工排查用）

告警渠道复用 openclaw 的飞书（~/.openclaw/openclaw.json）+ balance_alert 的告警群。
零依赖（仅标准库）。
"""
import os, sys, json, hashlib, glob, time, urllib.request

HOME = os.path.expanduser('~')
BASELINE = os.path.join(HOME, 'hq-drift', 'baseline.json')
STATE = os.path.join(HOME, 'hq-drift', '.state.json')
LOG = os.path.join(HOME, 'hq-drift', 'sentinel.log')
COOLDOWN = 6 * 3600  # 同一漂移集 6h 内不重复告警

WEBROOT = '/var/www/huangquechuanmei'
# 受监控的后端文件（git server/* → 线上路径）
SERVER_FILES = [
    '/home/ubuntu/auth-service/auth_server.py',
    '/home/ubuntu/content-api/content_api.py',
    '/home/ubuntu/content-api/imggen_api.py',
    '/home/ubuntu/content-api/leadgen_api.py',
    '/home/ubuntu/content-api/tikhub.py',
    '/home/ubuntu/dl-service/dl_service.py',
]

# 后端目录：整目录下的 .py 都监控（T8 拆分后业务逻辑在 content_domains/）
SERVER_GLOBS = [
    '/home/ubuntu/content-api/content_domains/*.py',
]


def monitored_files():
    """当前应监控的文件全集：webroot(排除 assets/ 与 .bak) + 后端文件。"""
    files = []
    for p in glob.glob(os.path.join(WEBROOT, '**', '*'), recursive=True):
        if not os.path.isfile(p):
            continue
        rel = os.path.relpath(p, WEBROOT)
        if rel.startswith('assets' + os.sep) or '.bak' in os.path.basename(p):
            continue
        files.append(p)
    files += [p for p in SERVER_FILES if os.path.isfile(p)]
    for pat in SERVER_GLOBS:
        files += [p for p in glob.glob(pat) if os.path.isfile(p) and '__pycache__' not in p]
    return sorted(set(files))


def md5_norm(path):
    """剔除 CRLF 后算 md5，避免换行符差异误报。"""
    try:
        with open(path, 'rb') as f:
            data = f.read().replace(b'\r\n', b'\n')
        return hashlib.md5(data).hexdigest()
    except Exception:
        return None


def snapshot():
    return {p: md5_norm(p) for p in monitored_files()}


def log(msg):
    line = time.strftime('%Y-%m-%d %H:%M:%S ') + msg
    try:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass
    print(line)


# ---------- 飞书告警（复用 balance_alert 的路径）----------
def _feishu_send(text):
    try:
        fe = json.load(open(os.path.join(HOME, '.openclaw', 'openclaw.json')))['channels']['feishu']
        aid, sec = fe['appId'], fe['appSecret']
    except Exception as e:
        log('读飞书配置失败: %s' % e); return False
    gid = None
    try:
        for b in json.load(open(os.path.join(HOME, 'agent-metrics', 'bot_groups.json'))):
            for g in b['in_groups']:
                if g['name'] == '父OpenClaw开发测试':
                    gid = g['id']
    except Exception:
        pass
    if not gid:
        log('未找到告警群 chat_id'); return False
    try:
        tok = json.load(urllib.request.urlopen(urllib.request.Request(
            'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
            data=json.dumps({'app_id': aid, 'app_secret': sec}).encode(),
            headers={'Content-Type': 'application/json'}), timeout=10)).get('tenant_access_token')
        body = json.dumps({'receive_id': gid, 'msg_type': 'text',
                           'content': json.dumps({'text': text})}).encode()
        r = json.load(urllib.request.urlopen(urllib.request.Request(
            'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id',
            data=body, headers={'Authorization': 'Bearer ' + tok,
                                'Content-Type': 'application/json'}), timeout=10))
        return r.get('code') == 0
    except Exception as e:
        log('飞书发送失败: %s' % e); return False


def bless():
    os.makedirs(os.path.dirname(BASELINE), exist_ok=True)
    snap = snapshot()
    json.dump({'blessed_at': int(time.time()), 'files': snap},
              open(BASELINE, 'w', encoding='utf-8'), ensure_ascii=False, indent=0)
    log('基线已重建：%d 个文件' % len(snap))


def diff():
    if not os.path.exists(BASELINE):
        return None
    base = json.load(open(BASELINE, encoding='utf-8'))['files']
    cur = snapshot()
    changed = [p for p in cur if p in base and cur[p] != base[p]]
    missing = [p for p in base if p not in cur]
    added = [p for p in cur if p not in base]
    return {'changed': sorted(changed), 'missing': sorted(missing), 'added': sorted(added)}


def main():
    if '--test' in sys.argv:
        print('飞书自检:', _feishu_send('【漂移哨兵自检】黄雀主站文件漂移监测通道正常，可忽略 🙏'))
        return
    if '--bless' in sys.argv:
        bless(); return
    d = diff()
    if d is None:
        log('无基线，请先运行 --bless'); return
    total = len(d['changed']) + len(d['missing']) + len(d['added'])
    if total == 0:
        log('巡检正常：线上与基线一致，无漂移'); return

    def short(p):
        return p.replace(WEBROOT, 'webroot').replace('/home/ubuntu', '~')
    lines = ['⚠️ 黄雀主站检测到 %d 处文件漂移（有人可能绕过 ship 直接改了服务器）' % total]
    for tag, key in (('改动', 'changed'), ('删除', 'missing'), ('新增', 'added')):
        if d[key]:
            lines.append('【%s %d】%s' % (tag, len(d[key]), '、'.join(short(p) for p in d[key][:12])))
    lines.append('→ 如是正常部署请走 ship 并在部署后 `drift_sentinel.py --bless`；如非本人所为请排查。')
    msg = '\n'.join(lines)
    log('检测到漂移: changed=%d missing=%d added=%d' % (len(d['changed']), len(d['missing']), len(d['added'])))

    if '--print' in sys.argv:
        print(msg); return
    # 冷却：同一漂移指纹 6h 内只报一次
    fp = hashlib.md5(msg.encode('utf-8')).hexdigest()
    st = json.load(open(STATE)) if os.path.exists(STATE) else {}
    if st.get('fp') == fp and time.time() - st.get('ts', 0) < COOLDOWN:
        log('漂移未变且在冷却期内，跳过告警'); return
    ok = _feishu_send(msg)
    json.dump({'fp': fp, 'ts': int(time.time())}, open(STATE, 'w'))
    log('已发飞书告警: %s' % ok)


if __name__ == '__main__':
    main()
