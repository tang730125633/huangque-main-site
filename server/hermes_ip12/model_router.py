"""模型路由层 - 多API自动切换"""
import os
import requests as http_requests
import time

PROVIDERS = [
    {"name": "zelong GPT-5.4-mini", "base_url": os.environ.get("HERMES_ZELONG_API_BASE", "https://api.zelong.vip/v1"), "api_key": os.environ.get("HERMES_ZELONG_API_KEY", ""), "model": os.environ.get("HERMES_ZELONG_MINI_MODEL", "gpt-5.4-mini"), "timeout": 60},
    {"name": "zelong GPT-5.4", "base_url": os.environ.get("HERMES_ZELONG_API_BASE", "https://api.zelong.vip/v1"), "api_key": os.environ.get("HERMES_ZELONG_API_KEY", ""), "model": os.environ.get("HERMES_ZELONG_MODEL", "gpt-5.4"), "timeout": 60},
    {"name": "gptsapi GPT-4o", "base_url": os.environ.get("HERMES_GPTSAPI_BASE", "https://api.gptsapi.net/v1"), "api_key": os.environ.get("HERMES_GPTSAPI_KEY", ""), "model": os.environ.get("HERMES_GPTSAPI_MODEL", "gpt-4o"), "timeout": 60},
]
PROVIDERS = [provider for provider in PROVIDERS if provider["api_key"]]
_current = 0
_fails = {}
_cooldown = {}

def call_ai(messages, stream=False, temperature=0.7, max_retries=None):
    global _current
    if not PROVIDERS:
        raise RuntimeError("No Hermes AI provider is configured")
    if max_retries is None: max_retries = len(PROVIDERS)
    last_err = None
    for attempt in range(max_retries):
        idx = (_current + attempt) % len(PROVIDERS)
        p = PROVIDERS[idx]
        now = time.time()
        if idx in _cooldown and now < _cooldown[idx]: continue
        try:
            resp = http_requests.post(f"{p['base_url']}/chat/completions",
                headers={"Authorization":f"Bearer {p['api_key']}","Content-Type":"application/json"},
                json={"model":p["model"],"messages":messages,"stream":stream,"temperature":temperature},
                timeout=p["timeout"],stream=stream)
            if resp.status_code == 200:
                _current = idx; _fails[idx] = 0; return resp
            if resp.status_code == 429: _cooldown[idx] = now + 30
            elif resp.status_code in [401,403]: _cooldown[idx] = now + 3600
            elif resp.status_code >= 500: _cooldown[idx] = now + 10
            _fails[idx] = _fails.get(idx,0) + 1
            last_err = Exception(f"[{p['name']}] HTTP {resp.status_code}")
        except Exception as e:
            _fails[idx] = _fails.get(idx,0) + 1
            _cooldown[idx] = now + 15
            last_err = Exception(f"[{p['name']}] {str(e)[:80]}")
    raise Exception(f"All providers failed: {last_err}")

def current_provider(): return PROVIDERS[_current]["name"] if PROVIDERS else "unconfigured"

def provider_status():
    status = []; now = time.time()
    for i,p in enumerate(PROVIDERS):
        s = {"name":p["name"],"active":i==_current,"failures":_fails.get(i,0)}
        if i in _cooldown and now < _cooldown[i]: s["cooldown"] = int(_cooldown[i]-now)
        status.append(s)
    return status
