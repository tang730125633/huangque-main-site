#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黄雀主站「漂移哨兵」——检测有人绕过 ship 直接热改服务器文件。

巡检不再依赖上次 bless 的基线，而是直接比较：
  线上运行文件 == 服务器 checkout 中的 git origin/main 文件

这样即使有人错误执行过 bless，只要线上与 git 不一致仍会报警。

用法：
  drift_sentinel.py                         巡检(cron 用)，有漂移则飞书告警(带冷却)
  drift_sentinel.py --print                 只打印漂移，不告警
  drift_sentinel.py --bless                 兼容旧调用：记录当前 origin/main 应有清单
  drift_sentinel.py --bless-deploy file...  记录本次 ship 部署的文件清单，不改变巡检判断
  drift_sentinel.py --verify-deploy file... 校验本次 ship 文件线上 == git origin/main
  drift_sentinel.py --test                  发一条飞书自检消息

告警渠道复用 openclaw 的飞书（~/.openclaw/openclaw.json）+ balance_alert 的告警群。
零依赖（仅标准库）。
"""
import argparse
import glob
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

HOME = os.path.expanduser('~')
DRIFT_DIR = os.path.join(HOME, 'hq-drift')
BASELINE = os.path.join(DRIFT_DIR, 'baseline.json')
DEPLOY_LOG = os.path.join(DRIFT_DIR, 'deploy_bless.jsonl')
STATE = os.path.join(DRIFT_DIR, '.state.json')
LOG = os.path.join(DRIFT_DIR, 'sentinel.log')
COOLDOWN = 6 * 3600

WEBROOT = os.environ.get('HQ_WEBROOT', '/var/www/huangquechuanmei')
REPO = os.environ.get('HQ_REPO', os.path.join(HOME, 'huangque-main-site'))
GIT_REF = os.environ.get('HQ_DRIFT_REF', 'origin/main')

BACKEND_RUNTIME = {
    'server/auth_server.py': '/home/ubuntu/auth-service/auth_server.py',
    'server/wxpay.py': '/home/ubuntu/auth-service/wxpay.py',
    'server/wechat_virtual_pay.py': '/home/ubuntu/auth-service/wechat_virtual_pay.py',
    'server/sync_virtual_pay_goods.py': '/home/ubuntu/auth-service/sync_virtual_pay_goods.py',
    'server/content_api.py': '/home/ubuntu/content-api/content_api.py',
    'server/imggen_api.py': '/home/ubuntu/content-api/imggen_api.py',
    'server/leadgen_api.py': '/home/ubuntu/content-api/leadgen_api.py',
    'server/tikhub.py': '/home/ubuntu/content-api/tikhub.py',
    # func_names：content 和 admin 【两个服务】都 import 它。漏部署 = 两个服务一起起不来。
    'server/func_names.py': '/home/ubuntu/content-api/func_names.py',
    'server/dl_service.py': '/home/ubuntu/dl-service/dl_service.py',
    'server/admin_api.py': '/home/ubuntu/content-api/admin_api.py',
    'scripts/drift_sentinel.py': '/home/ubuntu/hq-drift/drift_sentinel.py',
}

CONTENT_DOMAINS_RUNTIME = os.environ.get('HQ_CONTENT_DOMAINS_RUNTIME', '/home/ubuntu/content-api/content_domains')
SYSTEMD_DIR = os.environ.get('HQ_SYSTEMD_DIR', '/etc/systemd/system')


def log(msg):
    line = time.strftime('%Y-%m-%d %H:%M:%S ') + msg
    try:
        os.makedirs(DRIFT_DIR, exist_ok=True)
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass
    print(line)


def norm_bytes(data):
    return data.replace(b'\r\n', b'\n')


def md5_bytes(data):
    return hashlib.md5(norm_bytes(data)).hexdigest()


def md5_file(path):
    try:
        with open(path, 'rb') as f:
            return md5_bytes(f.read())
    except Exception:
        return None


def git(args, check=True):
    return subprocess.run(
        ['git', '-C', REPO] + list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def ensure_repo_ref():
    if not os.path.isdir(os.path.join(REPO, '.git')):
        raise RuntimeError('git checkout 不存在：%s' % REPO)
    git(['fetch', '--quiet', 'origin'], check=False)
    git(['rev-parse', '--verify', GIT_REF], check=True)


def git_ls_tree(prefix):
    p = git(['ls-tree', '-r', '--name-only', GIT_REF, prefix], check=False)
    if p.returncode != 0:
        return []
    return [x.strip() for x in p.stdout.decode('utf-8', 'ignore').splitlines() if x.strip()]


def git_md5(git_path):
    p = git(['show', '%s:%s' % (GIT_REF, git_path)], check=False)
    if p.returncode != 0:
        return None
    return md5_bytes(p.stdout)


def git_path_to_runtime(git_path):
    git_path = git_path.replace('\\', '/')
    if git_path.startswith('site/'):
        return os.path.join(WEBROOT, git_path[len('site/'):])
    if git_path in BACKEND_RUNTIME:
        return BACKEND_RUNTIME[git_path]
    if git_path.startswith('server/content_domains/') and git_path.endswith('.py'):
        return os.path.join(CONTENT_DOMAINS_RUNTIME, os.path.basename(git_path))
    if git_path.startswith('deploy/systemd/'):
        # ship 现在会部署 systemd 单元与 drop-in。没有这段映射，--verify-deploy 会把它们静默跳过，
        # 然后报「N 个文件校验通过」—— 一个虚假的确认，比不校验更糟。
        rel = git_path[len('deploy/systemd/'):]
        if rel.endswith('.example'):
            return None          # 示例文件不落地（doubao.conf 含明文密钥，真值只在服务器上）
        return os.path.join(SYSTEMD_DIR, rel)
    return None


def runtime_to_git_path(path):
    path = os.path.normpath(path)
    webroot = os.path.normpath(WEBROOT)
    if path.startswith(webroot + os.sep):
        rel = os.path.relpath(path, webroot).replace(os.sep, '/')
        if rel.startswith('assets/') or '.bak' in os.path.basename(rel):
            return None
        return 'site/' + rel
    for git_path, runtime in BACKEND_RUNTIME.items():
        if path == os.path.normpath(runtime):
            return git_path
    domain_dir = os.path.normpath(CONTENT_DOMAINS_RUNTIME)
    if path.startswith(domain_dir + os.sep) and path.endswith('.py') and '__pycache__' not in path:
        return 'server/content_domains/' + os.path.basename(path)
    systemd_dir = os.path.normpath(SYSTEMD_DIR)
    if path.startswith(systemd_dir + os.sep):
        return 'deploy/systemd/' + os.path.relpath(path, systemd_dir).replace(os.sep, '/')
    return None


def expected_git_paths():
    paths = []
    for p in git_ls_tree('site'):
        if p.startswith('site/assets/') or '.bak' in os.path.basename(p):
            continue
        paths.append(p)
    for p in BACKEND_RUNTIME:
        if git_md5(p) is not None:
            paths.append(p)
    for p in git_ls_tree('server/content_domains'):
        if p.endswith('.py') and '__pycache__' not in p:
            paths.append(p)
    return sorted(set(paths))


def runtime_files():
    files = []
    for p in glob.glob(os.path.join(WEBROOT, '**', '*'), recursive=True):
        if os.path.isfile(p) and runtime_to_git_path(p):
            files.append(p)
    for p in BACKEND_RUNTIME.values():
        if os.path.isfile(p):
            files.append(p)
    for p in glob.glob(os.path.join(CONTENT_DOMAINS_RUNTIME, '*.py')):
        if os.path.isfile(p) and runtime_to_git_path(p):
            files.append(p)
    return sorted(set(files))


def diff_paths(git_paths=None):
    ensure_repo_ref()
    wanted = sorted(set(git_paths or expected_git_paths()))
    changed, missing, added = [], [], []
    expected_runtime = {}
    for gp in wanted:
        rp = git_path_to_runtime(gp)
        if rp:
            expected_runtime[gp] = rp
            if not os.path.exists(rp):
                missing.append(gp)
                continue
            if md5_file(rp) != git_md5(gp):
                changed.append(gp)

    if git_paths is None:
        expected_git = set(expected_runtime)
        for rp in runtime_files():
            gp = runtime_to_git_path(rp)
            if gp and gp not in expected_git and git_md5(gp) is None:
                added.append(gp)

    return {
        'changed': sorted(changed),
        'missing': sorted(missing),
        'added': sorted(set(added)),
    }


def snapshot():
    ensure_repo_ref()
    return {
        gp: {
            'runtime': git_path_to_runtime(gp),
            'git_md5': git_md5(gp),
            'runtime_md5': md5_file(git_path_to_runtime(gp)) if git_path_to_runtime(gp) else None,
        }
        for gp in expected_git_paths()
    }


def bless(paths=None):
    os.makedirs(DRIFT_DIR, exist_ok=True)
    paths = [p.replace('\\', '/') for p in (paths or [])]
    if paths:
        d = diff_paths(paths)
        record = {
            'deployed_at': int(time.time()),
            'git_ref': GIT_REF,
            'repo': REPO,
            'files': paths,
            'verify': d,
        }
        with open(DEPLOY_LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
        log('部署 bless 已记录：%d 个文件' % len(paths))
        return
    snap = snapshot()
    with open(BASELINE, 'w', encoding='utf-8') as f:
        json.dump({'blessed_at': int(time.time()), 'git_ref': GIT_REF, 'files': snap}, f, ensure_ascii=False, indent=0)
    log('git 基线清单已记录：%d 个文件（巡检仍以 %s 为准）' % (len(snap), GIT_REF))


def short(git_path):
    return git_path


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


def format_diff(d):
    total = len(d['changed']) + len(d['missing']) + len(d['added'])
    lines = ['⚠️ 黄雀主站检测到 %d 处文件漂移（线上与 git %s 不一致）' % (total, GIT_REF)]
    for tag, key in (('改动', 'changed'), ('删除', 'missing'), ('新增', 'added')):
        if d[key]:
            lines.append('【%s %d】%s' % (tag, len(d[key]), '、'.join(short(p) for p in d[key][:12])))
    lines.append('→ 请勿直接改服务器；正常上线必须走 PR 合并后由审核方执行 ship。')
    return '\n'.join(lines)


def handle_detect(print_only=False):
    d = diff_paths()
    total = len(d['changed']) + len(d['missing']) + len(d['added'])
    if total == 0:
        log('巡检正常：线上与 git %s 一致，无漂移' % GIT_REF)
        return 0
    msg = format_diff(d)
    log('检测到漂移: changed=%d missing=%d added=%d' % (len(d['changed']), len(d['missing']), len(d['added'])))
    if print_only:
        print(msg)
        return 1
    fp = hashlib.md5(msg.encode('utf-8')).hexdigest()
    st = json.load(open(STATE)) if os.path.exists(STATE) else {}
    if st.get('fp') == fp and time.time() - st.get('ts', 0) < COOLDOWN:
        log('漂移未变且在冷却期内，跳过告警')
        return 1
    ok = _feishu_send(msg)
    os.makedirs(DRIFT_DIR, exist_ok=True)
    json.dump({'fp': fp, 'ts': int(time.time())}, open(STATE, 'w'))
    log('已发飞书告警: %s' % ok)
    return 1


def handle_verify(paths):
    paths = [p.replace('\\', '/') for p in paths]
    d = diff_paths(paths)
    total = len(d['changed']) + len(d['missing']) + len(d['added'])
    if total == 0:
        log('部署后校验通过：%d 个文件线上 == git %s' % (len(paths), GIT_REF))
        return 0
    print(format_diff(d))
    log('部署后校验失败：changed=%d missing=%d added=%d' % (len(d['changed']), len(d['missing']), len(d['added'])))
    return 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--test', action='store_true')
    ap.add_argument('--print', dest='print_only', action='store_true')
    ap.add_argument('--bless', action='store_true')
    ap.add_argument('--bless-deploy', nargs='*')
    ap.add_argument('--verify-deploy', nargs='*')
    args = ap.parse_args()

    if args.test:
        print('飞书自检:', _feishu_send('【漂移哨兵自检】黄雀主站文件漂移监测通道正常，可忽略'))
        return 0
    if args.verify_deploy is not None:
        return handle_verify(args.verify_deploy)
    if args.bless_deploy is not None:
        bless(args.bless_deploy)
        return 0
    if args.bless:
        bless()
        return 0
    return handle_detect(args.print_only)


if __name__ == '__main__':
    sys.exit(main())
