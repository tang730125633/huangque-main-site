import sys; sys.path.insert(0,'server')
import paramiko
REMOTE = r'''
cd /home/ubuntu/content-api
set -a; . ./content.env 2>/dev/null; set +a
python3 - <<'PYEOF'
import json
from content_domains import channel_manager as cm, channel_parameters as cp
for cid in ('xlw-image-2','xlw-image-25'):
    try:
        cfg=cm.version(cid)
    except Exception as e:
        print("  %s 读取失败: %s" % (cid, e)); continue
    print("=== %s ===" % cid)
    print("  adapter=%s model=%s" % (cfg.get('adapter'), cfg.get('model')))
    print("  parameters=", json.dumps(cfg.get('parameters'), ensure_ascii=False)[:300])
    try:
        cap=cp.capabilities(cfg)
        print("  capabilities: profile=%s fields=%s" % (cap.get('profile'), list((cap.get('fields') or {}).keys())))
        for k,v in (cap.get('fields') or {}).items():
            print("     %-14s %s" % (k, str(v)[:90]))
    except Exception as e:
        print("  capabilities 失败:", type(e).__name__, str(e)[:140])
    print()
    try:
        print("  apply(最小载荷) 结果:", json.dumps(cp.apply(cfg, {'prompt':'x','ratio':'1:1','quality':'std','count':1})[:1] if False else str(cp.apply(cfg, {'prompt':'x','ratio':'1:1','quality':'std','count':1}))[:300], ensure_ascii=False))
    except Exception as e:
        print("  apply 失败 →", type(e).__name__, str(e)[:220])
    print()
