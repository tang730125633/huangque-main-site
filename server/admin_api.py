#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Huangque operations admin API.

Stage 1 covers service/key/channel visibility and read-only job statistics.
All /api/admin/* routes require a platform token whose user role is admin.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import closing
from http import cookies
import json
import os
import pathlib
import re

try:
    import func_names                    # 生产：admin_api.py 直接跑，同目录下就是 func_names.py
except ModuleNotFoundError:              # 测试：以包的形式 import server.admin_api，server/ 不在 sys.path 上
    from . import func_names
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from content_domains import feature_flags
except ImportError:
    feature_flags = None

AUTH_COOKIE_NAME = os.environ.get("HQ_AUTH_COOKIE_NAME", "hq_session")

def request_token(headers):
    auth = headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
        if token and token != "__cookie__":
            return token
    try:
        jar = cookies.SimpleCookie()
        jar.load(headers.get("Cookie") or "")
        morsel = jar.get(AUTH_COOKIE_NAME)
        return morsel.value.strip() if morsel and morsel.value else ""
    except Exception:
        return ""


BASE = pathlib.Path(__file__).resolve().parent
PORT = int(os.environ.get("ADMIN_API_PORT", "8099"))
AUTH_BASE = os.environ.get("AUTH_BASE", "http://127.0.0.1:8095").rstrip("/")
AUTH_INTERNAL_TOKEN = os.environ.get("HQ_INTERNAL_TOKEN", "")
JOB_DB = pathlib.Path(os.environ.get("CONTENT_JOB_DB", str(BASE / "content_jobs.db")))
ADMIN_DB = pathlib.Path(os.environ.get("ADMIN_DB", str(BASE / "admin_config.db")))

ENV_FILES = [
    pathlib.Path("/home/ubuntu/content-api/content.env"),
    pathlib.Path("/home/ubuntu/content-api/runninghub.env"),
    pathlib.Path("/etc/huangque/runninghub.env"),
    pathlib.Path("/home/ubuntu/content-api/whisper.env"),
    pathlib.Path("/home/ubuntu/auth-service/auth.env"),
    pathlib.Path("/etc/leadgen-secrets.env"),
]

SERVICES = [
    {
        "key": "auth",
        "name": "认证服务",
        "port": 8095,
        "service_file": "deploy/systemd/huangque-auth.service",
        "health_url": "http://127.0.0.1:8095/api/auth/health",
    },
    {
        "key": "content",
        "name": "内容生成服务",
        "port": 8096,
        "service_file": "deploy/systemd/huangque-content.service",
        "health_url": "http://127.0.0.1:8096/api/gen/health",
    },
    {
        "key": "imggen",
        "name": "作图服务",
        "port": 8101,
        "service_file": "deploy/systemd/huangque-imggen-api.service",
        "health_url": "http://127.0.0.1:8101/api/gen/banana/health",
    },
    {
        "key": "leadgen",
        "name": "获客采集服务",
        "port": 8100,
        "service_file": "deploy/systemd/huangque-leadgen-api.service",
        "health_url": "http://127.0.0.1:8100/api/gen/leadgen/health",
    },
    {
        "key": "dl",
        "name": "下载代理服务",
        "port": 8097,
        "service_file": "deploy/systemd/huangque-dl.service",
        "health_url": "http://127.0.0.1:8097/api/gen/dl/health",
    },
    {
        "key": "xiaotan",
        "name": "小探深采服务(抖音下载/ASR)",
        "port": 8501,
        "service_file": "服务器 systemd: xiaotan(docker)",
        # 只监听 docker 网桥 172.17.0.1,探 127.0.0.1 会误报离线(hq-monitor 的老坑)
        "health_url": "http://172.17.0.1:8501/docs",
    },
]

# 服务器实际在用的全部外部 API（来源:content.env/runninghub.env 变量盘点 2026-07-09）
KEY_GROUPS = [
    {"key": "openai", "name": "黄雀引擎 2 生图（经泽龙中转）", "env": ["OPENAI_API_KEY"]},
    {"key": "gemini", "name": "纳米香蕉生图", "env": ["GEMINI_API_KEY"]},
    {"key": "zelong", "name": "泽龙生图渠道1(小乐AI)", "env": ["ZELONG_KEY"]},
    {"key": "zelong2", "name": "泽龙生图渠道2(zelong.vip)", "env": ["ZELONG2_KEY"]},
    {"key": "heygen", "name": "HeyGen 数字人视频(直连)", "env": ["HEYGEN_API_KEY"]},
    {"key": "heygen_relay", "name": "HeyGen 中转(泽龙 relay)", "env": ["HEYGEN_RELAY_TOKEN"]},
    {"key": "xiaolevideo", "name": "小乐视频生成", "env": ["XIAOLEVIDEO_API_KEY"]},
    {"key": "runninghub", "name": "RunningHub 换装/动作模仿(线路一)", "env": ["RUNNINGHUB_API_KEY", "RUNNINGHUB_KEY"]},
    {"key": "wavespeed", "name": "WaveSpeed 换装(线路二)", "env": ["WAVESPEED_API_KEY"]},
    {"key": "doubao", "name": "豆包语音 TTS/声音克隆", "env": ["DOUBAO_APP_ID", "DOUBAO_ACCESS_TOKEN", "DOUBAO_APPID", "DOUBAO_TOKEN"]},
    {"key": "tikhub", "name": "TikHub 抖音数据", "env": ["TIKHUB_KEY", "TIKHUB_API_KEY"]},
    {"key": "cos", "name": "腾讯云 COS 存储", "env": ["COS_SECRET_ID", "COS_SECRET_KEY", "COS_REGION", "COS_BUCKET"]},
]

# 各渠道实际在用的业务接口清单(2026-07-09 全代码扫描产出,展示用;fee=调用计费)
ENDPOINT_CATALOG = json.loads(r"""
{
 "tikhub": [
  {
   "m": "POST",
   "p": "/api/v1/douyin/search/fetch_general_search_v1",
   "d": "抖音关键词综合搜索视频",
   "fee": true
  },
  {
   "m": "GET",
   "p": "/api/v1/douyin/web/fetch_one_video?aweme_id={aweme_id}",
   "d": "抖音视频详情",
   "fee": true
  },
  {
   "m": "GET",
   "p": "/api/v1/douyin/web/fetch_video_comments?aweme_id={aweme_id}&cursor={cu",
   "d": "抖音视频评论区抓取",
   "fee": true
  },
  {
   "m": "GET",
   "p": "/api/v1/xiaohongshu/app_v2/search_notes?keyword={keyword}&page={page}&",
   "d": "小红书关键词搜索笔记",
   "fee": true
  },
  {
   "m": "GET",
   "p": "/api/v1/xiaohongshu/app_v2/get_image_note_detail?note_id={note_id}",
   "d": "小红书图文笔记详情",
   "fee": true
  },
  {
   "m": "GET",
   "p": "/api/v1/xiaohongshu/app_v2/get_video_note_detail?note_id={note_id}",
   "d": "小红书视频笔记详情",
   "fee": true
  },
  {
   "m": "GET",
   "p": "/api/v1/xiaohongshu/app_v2/get_note_comments?note_id={note_id}",
   "d": "小红书笔记评论区抓取",
   "fee": true
  },
  {
   "m": "POST",
   "p": "/api/v1/wechat_channels/v2/fetch_channel_id_to_username",
   "d": "视频号sph短号→finder username",
   "fee": true
  },
  {
   "m": "POST",
   "p": "/api/v1/wechat_channels/v2/fetch_user_videos",
   "d": "视频号指定账号的视频列表",
   "fee": true
  },
  {
   "m": "POST",
   "p": "/api/v1/wechat_channels/v2/fetch_video_detail",
   "d": "视频号视频详情",
   "fee": true
  },
  {
   "m": "GET",
   "p": "/api/v1/douyin/app/v3/fetch_share_info_by_share_code?share_code={share",
   "d": "抖音口令式分享解析aweme_id",
   "fee": true
  },
  {
   "m": "GET",
   "p": "/api/v1/douyin/web/get_aweme_id?url={url}",
   "d": "抖音短链/分享链解析出aweme_id",
   "fee": true
  },
  {
   "m": "GET",
   "p": "/api/v1/xiaohongshu/app/extract_share_info?share_link={share_link}",
   "d": "小红书分享链/短链解析出note_id",
   "fee": true
  },
  {
   "m": "GET",
   "p": "/api/v1/tikhub/user/get_user_info",
   "d": "TikHub账户信息/余额查询",
   "fee": false
  }
 ],
 "openai": [
  {
   "m": "POST",
   "p": "/v1/audio/transcriptions",
   "d": "口播音频转写ASR",
   "fee": true
  },
  {
   "m": "POST",
   "p": "/v1/images/generations",
   "d": "黄雀引擎 2 文生图",
   "fee": true
  },
  {
   "m": "POST",
   "p": "/v1/images/edits",
   "d": "黄雀引擎 2 图生图/局部修改",
   "fee": true
  },
  {
   "m": "POST",
   "p": "/v1/audio/speech",
   "d": "OpenAI TTS 配音",
   "fee": true
  },
  {
   "m": "POST",
   "p": "/v1/chat/completions",
   "d": "营销文案/分镜脚本生成",
   "fee": true
  }
 ],
 "xiaolevideo": [
  {
   "m": "POST",
   "p": "/api/v1/generations",
   "d": "黄雀引擎 2 文生图/图生图 创建生成任务",
   "fee": true
  },
  {
   "m": "GET",
   "p": "/api/v1/generations/{request_id}",
   "d": "轮询果肉生图任务状态",
   "fee": false
  },
  {
   "m": "GET",
   "p": "{渠道返回的图片url}",
   "d": "下载果肉渠道返回的生成图片",
   "fee": false
  },
  {
   "m": "GET",
   "p": "{xiaole成片CDN URL}；非 .cn 域(如 vidgen.x.ai)改写为 {HEYGEN_RELAY_BASE}/cdn/{h",
   "d": "下载果肉/豆姐成片 mp4 落盘",
   "fee": false
  }
 ],
 "zelong": [
  {
   "m": "POST",
   "p": "/v1/images/generations",
   "d": "黄雀引擎 2 文生图",
   "fee": true
  },
  {
   "m": "POST",
   "p": "/v1/images/edits",
   "d": "黄雀引擎 2 图生图/局部修改",
   "fee": true
  }
 ],
 "zelong2": [
  {
   "m": "POST",
   "p": "/image-pool/v1/images/generations",
   "d": "黄雀引擎 2 文生图",
   "fee": true
  },
  {
   "m": "POST",
   "p": "/image-pool/v1/images/edits",
   "d": "黄雀引擎 2 图生图/局部修改",
   "fee": true
  }
 ],
 "gemini": [
  {
   "m": "POST",
   "p": "/v1beta/models/gemini-3.1-flash-image:generateContent",
   "d": "纳米香蕉 2 作图",
   "fee": true
  }
 ],
 "cos": [
  {
   "m": "PUT",
   "p": "/{COS_PREFIX}/{filename}",
   "d": "banana 出图上传 COS 返回直链",
   "fee": true
  },
  {
   "m": "PUT",
   "p": "{bucket}.cos.ap-guangzhou.myqcloud.com/collect/{platform}/{id}.mp4",
   "d": "采集视频转存 COS 永久直链",
   "fee": true
  },
  {
   "m": "PUT",
   "p": "/{object_key}",
   "d": "产出文件上传 COS 返回公开或签名直链",
   "fee": true
  }
 ],
 "heygen": [
  {
   "m": "POST",
   "p": "/assets",
   "d": "上传素材换取 asset_id，multipart 上传",
   "fee": false
  },
  {
   "m": "POST",
   "p": "/videos",
   "d": "数字化 IP 视频生成，泽龙中转路径",
   "fee": true
  },
  {
   "m": "POST",
   "p": "/avatars",
   "d": "用图片 asset 创建 Photo Avatar",
   "fee": false
  },
  {
   "m": "GET",
   "p": "/avatars/{avatar_group_id}",
   "d": "轮询单个 Photo Avatar 组处理状态",
   "fee": false
  },
  {
   "m": "GET",
   "p": "/avatars",
   "d": "轮询 avatar 列表判断 Photo Avatar 是否就绪",
   "fee": false
  },
  {
   "m": "GET",
   "p": "/videos/{video_id}",
   "d": "轮询视频生成状态",
   "fee": false
  },
  {
   "m": "GET",
   "p": "{heygen成片CDN URL}；命中 *.heygen.ai/*.heygen.com 时改写为 {HEYGEN_RELAY_BASE}",
   "d": "下载 HeyGen 成片 mp4 落盘",
   "fee": false
  },
  {
   "m": "POST",
   "p": "/v1/talking_photo",
   "d": "口播直连：上传人物形象图创建 talking_photo",
   "fee": false
  },
  {
   "m": "POST",
   "p": "/v1/asset",
   "d": "口播直连：上传豆包合成的 mp3 音频换 asset_id",
   "fee": false
  },
  {
   "m": "POST",
   "p": "/v2/video/generate",
   "d": "口播直连：talking_photo + 音频 asset 生成数字化 IP 视频",
   "fee": true
  },
  {
   "m": "GET",
   "p": "/v1/video_status.get?video_id={video_id}",
   "d": "口播直连：轮询生成状态",
   "fee": false
  }
 ],
 "runninghub": [
  {
   "m": "POST",
   "p": "https://www.runninghub.cn (SDK RunningHubClient.upload_file)",
   "d": "换装/换背景：上传人物视频、衣服图、背景图素材",
   "fee": false
  },
  {
   "m": "POST",
   "p": "https://www.runninghub.cn (SDK run_ai_app, webappId=196960511618784461",
   "d": "换装 AI App：人物视频+衣服图→换装视频",
   "fee": true
  },
  {
   "m": "POST",
   "p": "https://www.runninghub.cn (SDK run_ai_app, webappId=198635352148852326",
   "d": "换背景 AI App：视频+背景图→换背景视频",
   "fee": true
  },
  {
   "m": "POST",
   "p": "https://www.runninghub.cn (SDK get_status/{task_id})",
   "d": "轮询换装/换背景任务状态",
   "fee": false
  },
  {
   "m": "POST",
   "p": "https://www.runninghub.cn (SDK get_outputs/{task_id})",
   "d": "获取任务产出文件列表",
   "fee": false
  },
  {
   "m": "GET",
   "p": "{RunningHub outputs 文件URL} (SDK download_outputs)",
   "d": "下载换装/换背景成片到本地工作目录",
   "fee": false
  }
 ],
 "wavespeed": [
  {
   "m": "POST",
   "p": "/api/v3/wavespeed-ai/wan-2.2/animate",
   "d": "动作模仿(线路二)：人物图+参考视频→动作模仿视频",
   "fee": true
  },
  {
   "m": "POST",
   "p": "/api/v3/wavespeed-ai/ai-virtual-outfit-tryon",
   "d": "换装(线路二)：人物图+衣服图→模特展示视频",
   "fee": true
  },
  {
   "m": "GET",
   "p": "/api/v3/predictions/{id}/result",
   "d": "轮询 WaveSpeed 任务状态、取成片URL",
   "fee": false
  },
  {
   "m": "GET",
   "p": "/api/v3/balance",
   "d": "查询 WaveSpeed 账户余额(拨测/接口调试用)",
   "fee": false
  }
 ],
 "doubao": [
  {
   "m": "POST",
   "p": "/api/v3/tts/unidirectional/sse",
   "d": "豆包大模型 TTS 单向流式语音合成",
   "fee": true
  },
  {
   "m": "POST",
   "p": "/api/v1/mega_tts/audio/upload",
   "d": "豆包 VIP 声音克隆——上传样音启动音色复刻训练",
   "fee": true
  },
  {
   "m": "POST",
   "p": "/api/v1/mega_tts/status",
   "d": "查询豆包声音克隆训练状态",
   "fee": false
  }
 ]
}
""")

CHANNELS = {
    item["key"]: {
        "key": item["key"],
        "name": item["name"],
        "required_env": item["env"],
        "default_config": {"cost": "", "rate_limit": "", "defaults": ""},
    }
    for item in KEY_GROUPS
}

SECRET_RE = re.compile(r"(key|token|secret|password|passwd|pwd|credential)", re.I)

# 主站 vhost 单独写 huangquechuanmei.access.log；默认 access.log 只有 leadgen 等其他站
NGINX_ACCESS_LOGS = [
    pathlib.Path(p.strip())
    for p in os.environ.get(
        "NGINX_ACCESS_LOGS",
        "/var/log/nginx/huangquechuanmei.access.log,/var/log/nginx/access.log",
    ).split(",")
    if p.strip()
]
# nginx combined 格式：ip - user [time] "METHOD path HTTP/x" status size "referer" "ua"
# remote_user 可能带空格（basic auth），所以 ip 之后宽松匹配到第一个 [
LOG_LINE_RE = re.compile(
    r'^(?P<ip>\S+) [^\[]*\[(?P<time>[^\]]+)\] "(?P<method>[A-Z]+) (?P<path>\S+)[^"]*" '
    r'(?P<status>\d{3}) (?P<size>\d+|-) "[^"]*" "(?P<ua>[^"]*)"'
)
_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
# 按参数名打码：token=xxx、api_key=xxx，兼容 & ; 分隔；dk=视频号解密密钥(dl_service)
QUERY_SECRET_RE = re.compile(
    r"((?:^|[?&;])(?:[^&;=]*(?:key|token|secret|password|passwd|pwd|credential|sign)[^&;=]*|dk)=)[^&;]*",
    re.I,
)
# 噪音 = 采集 worker 每秒轮询 /api/claim + 本后台自己的请求
NOISE_PATH_RE = re.compile(r"^/api/(claim\b|admin/)")

JOB_PATH_RE = re.compile(r"^/api/gen/job/(\d+)")
# 路径 → 功能名、任务 → 功能名：都在 func_names 里（唯一事实来源，运营后台和用户消费明细共用）。
# job/ 路径另走任务库反查真实功能+用户。
_path_func = func_names.path_func


def _job_users(job_ids):
    """批量反查任务号 → (用户, 功能名)。查不到/库不在就空着。"""
    if not job_ids or not JOB_DB.exists():
        return {}
    marks = ",".join("?" * len(job_ids))
    try:
        with closing(sqlite3.connect(str(JOB_DB), timeout=10)) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT id, username, kind, substr(payload, 1, 4096) AS payload FROM jobs WHERE id IN (%s)" % marks,
                list(job_ids),
            ).fetchall()
    except Exception:
        return {}
    return {
        int(r["id"]): (r["username"] or "-", call_func_name(r["kind"], _job_payload(r["payload"])))
        for r in rows
    }

# 出墙代理（mihomo）：OpenAI/HeyGen 要走，TikHub/RunningHub 必须直连（代理转 Cloudflare 会挂）
PROXY_URL = (os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or "").strip()
DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PROXY_OPENER = (
    urllib.request.build_opener(urllib.request.ProxyHandler({"http": PROXY_URL, "https": PROXY_URL}))
    if PROXY_URL
    else DIRECT_OPENER
)


def db():
    ADMIN_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(ADMIN_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS admin_channel_config(
                channel TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                config TEXT NOT NULL DEFAULT '{}',
                updated_by TEXT,
                updated_at INTEGER NOT NULL
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS admin_audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )"""
        )
        c.commit()
    if feature_flags is not None:
        feature_flags.init_db()


def verify(token):
    if not token:
        return None
    try:
        req = urllib.request.Request(
            AUTH_BASE + "/api/auth/me",
            headers={"Authorization": "Bearer " + token},
        )
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.loads(r.read().decode("utf-8")).get("user")
    except Exception:
        return None


def auth_admin_request(path, token, method="GET", payload=None):
    if not AUTH_INTERNAL_TOKEN:
        raise RuntimeError("未配置内部点数接口密钥")
    data = None
    headers = {
        "Authorization": "Bearer " + (token or ""),
        "X-HQ-Internal-Token": AUTH_INTERNAL_TOKEN,
    }
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(AUTH_BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except Exception:
            body = {}
        err = RuntimeError(body.get("detail") or "auth admin request failed")
        err.status = e.code
        err.body = body
        raise err


def auth_admin_raw(path, token):
    if not AUTH_INTERNAL_TOKEN:
        raise RuntimeError("auth internal token not configured")
    req = urllib.request.Request(AUTH_BASE + path, headers={
        "Authorization": "Bearer " + (token or ""),
        "X-HQ-Internal-Token": AUTH_INTERNAL_TOKEN,
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read(), r.headers.get("Content-Type"), r.headers.get("Content-Disposition")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except Exception:
            body = {}
        err = RuntimeError(body.get("detail") or "auth admin export failed")
        err.status = e.code
        err.body = body
        raise err


def auth_error_response(handler, exc):
    status = int(getattr(exc, "status", 502) or 502)
    body = getattr(exc, "body", None) or {"detail": str(exc)[:180]}
    return handler._send(status, body)


def _read_env_file(path):
    values = {}
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def env_sources():
    sources = [{"name": "process env", "values": dict(os.environ)}]
    for path in ENV_FILES:
        values = _read_env_file(path)
        if values:
            sources.append({"name": str(path), "values": values})
    return sources


def key_status():
    sources = env_sources()
    items = []
    for item in KEY_GROUPS:
        found = []
        for env_name in item["env"]:
            for src in sources:
                value = (src["values"].get(env_name) or "").strip()
                if value:
                    found.append({"env": env_name, "source": src["name"]})
                    break
        configured = len(found) == len(item["env"])
        if item["key"] in {"runninghub", "tikhub", "doubao", "heygen_relay"}:
            configured = bool(found)
        items.append(
            {
                "key": item["key"],
                "name": item["name"],
                "configured": configured,
                "required_env": item["env"],
                "sources": found,
                "pingable": item["key"] in KEY_PINGS,
                "endpoints": ENDPOINT_CATALOG.get(item["key"], []),
            }
        )
    return items


def _env_value(names):
    """按 env 名顺序找第一个非空值。只用于内部拨测，绝不外传。"""
    sources = env_sources()
    for env_name in names:
        for src in sources:
            value = (src["values"].get(env_name) or "").strip()
            if value:
                return value
    # 兜底：文件里没有(如 RunningHub 密钥文件 /etc/huangque/runninghub.env 是 600 root，admin 以 ubuntu 跑读不到)，
    # 但本进程环境里有的(systemd drop-in 注入)也算，避免误报"密钥未配置"。
    for env_name in names:
        value = (os.environ.get(env_name) or "").strip()
        if value:
            return value
    return ""


_BALANCE_KEY_RE = re.compile(r"balance|remain|coin|quota|credit", re.I)


def _find_balance(detail, depth=0):
    """从拨测响应里递归找余额类数值字段（remaining_quota/remainCoins/balance…）。"""
    if depth > 3 or not isinstance(detail, dict):
        return None
    for k, v in detail.items():
        if _BALANCE_KEY_RE.search(str(k)):
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return v
            if isinstance(v, str) and v.replace(".", "", 1).isdigit():
                return float(v) if "." in v else int(v)
    for v in detail.values():
        if isinstance(v, dict):
            found = _find_balance(v, depth + 1)
            if found is not None:
                return found
    return None


def _ping_upstream(method, url, headers=None, body=None, proxied=False, timeout=12):
    """真实调一次上游 API。只返回状态码/耗时/错误摘要，绝不含密钥。"""
    opener = PROXY_OPENER if proxied else DIRECT_OPENER
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = dict(headers or {})
    # Python-urllib 默认 UA 会被 TikHub 等家的 Cloudflare 拦成 403
    headers.setdefault("User-Agent", "Mozilla/5.0 (huangque-admin healthcheck)")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    start = time.time()
    out = {"ok": False, "http_status": None, "latency_ms": None}
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read(4096)
            out["http_status"] = r.status
        out["latency_ms"] = int((time.time() - start) * 1000)
        out["ok"] = True
        # RunningHub/TikHub 这类 HTTP 永远 200、业务错误放 body.code 的，跟进一层
        try:
            detail = json.loads(raw.decode("utf-8"))
            if isinstance(detail, dict):
                code = detail.get("code")
                if code is not None and str(code) not in ("0", "200"):
                    out["ok"] = False
                    out["error"] = "业务码 %s: %s" % (code, str(detail.get("msg") or detail.get("message") or "")[:120])
                balance = _find_balance(detail)
                if balance is not None:
                    out["balance"] = balance
        except Exception:
            pass
    except urllib.error.HTTPError as e:
        out.update({"http_status": e.code, "latency_ms": int((time.time() - start) * 1000), "error": "HTTP %s" % e.code})
    except Exception as e:
        out.update({"latency_ms": int((time.time() - start) * 1000), "error": str(e)[:180]})
    out.setdefault("mode", "auth")
    return out


def _reach_ping(url, proxied=False):
    """连通性拨测：只验证能不能通、延迟多少。任何 HTTP 响应（含 403/404）都算可达。"""
    out = _ping_upstream("GET", url, proxied=proxied)
    if not out["ok"] and out.get("http_status"):
        out["ok"] = True
        out.pop("error", None)
    out["mode"] = "reach"
    return out


def _key_ping_openai():
    key = _env_value(["OPENAI_API_KEY"])
    if not key:
        return {"ok": False, "error": "密钥未配置"}
    base = (_env_value(["OPENAI_BASE"]) or "https://api.openai.com").rstrip("/")
    # 官方域名被墙走 mihomo；泽龙等国内中转必须直连
    return _ping_upstream(
        "GET",
        base + "/v1/models",
        headers={"Authorization": "Bearer " + key},
        proxied="api.openai.com" in base,
    )


def _key_ping_heygen():
    key = _env_value(["HEYGEN_API_KEY"])
    if not key:
        return {"ok": False, "error": "密钥未配置"}
    return _ping_upstream(
        "GET", "https://api.heygen.com/v2/user/remaining_quota", headers={"X-Api-Key": key}, proxied=True
    )


def _key_ping_tikhub():
    key = _env_value(["TIKHUB_KEY", "TIKHUB_API_KEY"])
    if not key:
        return {"ok": False, "error": "密钥未配置"}
    base = (_env_value(["TIKHUB_BASE"]) or "https://api.tikhub.io").rstrip("/")
    return _ping_upstream(
        "GET", base + "/api/v1/tikhub/user/get_user_info", headers={"Authorization": "Bearer " + key}, proxied=False
    )


def _key_ping_runninghub():
    key = _env_value(["RUNNINGHUB_API_KEY", "RUNNINGHUB_KEY"])
    if not key:
        return {"ok": False, "error": "密钥未配置"}
    return _ping_upstream(
        "POST",
        "https://www.runninghub.cn/uc/openapi/accountStatus",
        headers={"Content-Type": "application/json", "Host": "www.runninghub.cn"},
        body={"apikey": key},
        proxied=False,
    )


def _key_ping_gemini():
    key = _env_value(["GEMINI_API_KEY"])
    if not key:
        return {"ok": False, "error": "密钥未配置", "mode": "auth"}
    base = (_env_value(["GEMINI_BASE"]) or "https://generativelanguage.googleapis.com").rstrip("/")
    # 官方域名被墙走代理；heygen.zelong.vip 中转直连。密钥走 header 不进 URL
    return _ping_upstream(
        "GET", base + "/v1beta/models", headers={"x-goog-api-key": key}, proxied="googleapis.com" in base
    )


def _openai_compat_ping(key_names, base_names, default_base):
    key = _env_value(key_names)
    if not key:
        return {"ok": False, "error": "密钥未配置", "mode": "auth"}
    base = (_env_value(base_names) or default_base).rstrip("/")
    return _ping_upstream("GET", base + "/v1/models", headers={"Authorization": "Bearer " + key}, proxied=False)


def _key_ping_zelong():
    return _openai_compat_ping(["ZELONG_KEY"], ["ZELONG_BASE"], "https://api.xiaoleai.team")


def _key_ping_zelong2():
    return _openai_compat_ping(["ZELONG2_KEY"], ["ZELONG2_BASE"], "https://api.zelong.vip")


def _key_ping_heygen_relay():
    base = _env_value(["HEYGEN_RELAY_BASE"])
    if not base:
        return {"ok": False, "error": "中转地址未配置", "mode": "reach"}
    return _reach_ping(base)


def _key_ping_xiaolevideo():
    base = (_env_value(["XIAOLEVIDEO_API_BASE"]) or "https://api.xiaolevideo.cn").rstrip("/")
    return _reach_ping(base)


def _key_ping_wavespeed():
    key = _env_value(["WAVESPEED_API_KEY"])
    if not key:
        return {"ok": False, "error": "密钥未配置", "mode": "auth"}
    # 直连 balance 端点真调验密钥；200 即密钥有效。data.balance 为剩余额度。
    return _ping_upstream(
        "GET", "https://api.wavespeed.ai/api/v3/balance",
        headers={"Authorization": "Bearer " + key}, proxied=False,
    )


def _key_ping_doubao():
    return _reach_ping("https://openspeech.bytedance.com")


def _key_ping_cos():
    domain = _env_value(["COS_DOMAIN"])
    if not domain:
        bucket, region = _env_value(["COS_BUCKET"]), _env_value(["COS_REGION"])
        if not (bucket and region):
            return {"ok": False, "error": "COS 配置不全", "mode": "reach"}
        domain = "%s.cos.%s.myqcloud.com" % (bucket, region)
    if not domain.startswith("http"):
        domain = "https://" + domain
    return _reach_ping(domain)


# auth=真调上游验证密钥有效; reach=签名类/未知协议渠道,只测连通与延迟
KEY_PINGS = {
    "openai": _key_ping_openai,
    "gemini": _key_ping_gemini,
    "zelong": _key_ping_zelong,
    "zelong2": _key_ping_zelong2,
    "heygen": _key_ping_heygen,
    "heygen_relay": _key_ping_heygen_relay,
    "xiaolevideo": _key_ping_xiaolevideo,
    "runninghub": _key_ping_runninghub,
    "wavespeed": _key_ping_wavespeed,
    "doubao": _key_ping_doubao,
    "tikhub": _key_ping_tikhub,
    "cos": _key_ping_cos,
}


def _sanitize_path(raw):
    """请求路径里 token/key 类查询参数打码，不让密钥出现在后台页面。

    直接对原始串做正则替换（兼容 & 和 ; 分隔）；值里嵌套了带密钥的
    URL 编码串（如 url=https%3A%2F%2Fx%3Ftoken%3Dabc）时整值打码。
    """
    masked = QUERY_SECRET_RE.sub(r"\1***", raw)
    if "%" in masked and "?" in masked:
        for m in re.finditer(r"([?&;][^&;=]+=)([^&;]+)", masked):
            if QUERY_SECRET_RE.search("?" + urllib.parse.unquote(m.group(2))):
                masked = masked.replace(m.group(0), m.group(1) + "***")
    return masked


def _parse_log_time(raw):
    """'09/Jul/2026:08:41:19 +0800' → (排序元组, '07-09 08:41:19')。不依赖 locale。"""
    try:
        day = int(raw[0:2])
        mon = _MONTHS[raw[3:6]]
        year = int(raw[7:11])
        hh, mm, ss = int(raw[12:14]), int(raw[15:17]), int(raw[18:20])
        return (year, mon, day, hh, mm, ss), "%02d-%02d %02d:%02d:%02d" % (mon, day, hh, mm, ss)
    except Exception:
        return (0, 0, 0, 0, 0, 0), raw


def _tail_lines(path, max_bytes=2 * 1024 * 1024):
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        chunk = f.read().decode("utf-8", "ignore")
    lines = chunk.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # 掐掉可能被截断的首行
    return lines


def _collect_request_entries(limit, status="", q="", include_noise=False):
    """采集 nginx /api/ 请求 → (按时间倒序的 [(排序键, item)], 错误提示)。已做用户/功能反查。"""
    entries, message = [], None
    existing = [p for p in NGINX_ACCESS_LOGS if p.exists()]
    if not existing:
        return [], "找不到 %s（服务器上才有）" % ", ".join(str(p) for p in NGINX_ACCESS_LOGS)
    for log_path in existing:
        try:
            lines = _tail_lines(log_path)
        except Exception as e:
            return [], "读取 %s 失败: %s" % (log_path, str(e)[:120])
        for line in lines:
            m = LOG_LINE_RE.match(line)
            if not m:
                continue
            path = m.group("path")
            if not path.startswith("/api/"):
                continue
            if not include_noise and NOISE_PATH_RE.match(path):
                continue
            code = m.group("status")
            if status:
                # ok/fail = 统一语义(给合并时间线用)；单数字=状态码前缀；三位=精确
                if status == "ok":
                    if int(code) >= 400:
                        continue
                elif status == "fail":
                    if int(code) < 400:
                        continue
                elif code[:1] != status if len(status) == 1 else code != status:
                    continue
            if q and q not in path:
                continue
            sort_key, disp = _parse_log_time(m.group("time"))
            jid_match = JOB_PATH_RE.match(path)
            entries.append(
                (
                    sort_key,
                    {
                        "time": disp,
                        "user": "-",
                        "func": _path_func(path),
                        "ip": m.group("ip"),
                        "method": m.group("method"),
                        "path": _sanitize_path(path),
                        "status": int(code),
                        "size": 0 if m.group("size") == "-" else int(m.group("size")),
                        "ua": m.group("ua")[:120],
                        "_jid": int(jid_match.group(1)) if jid_match else None,
                    },
                )
            )
    entries.sort(key=lambda x: x[0], reverse=True)
    entries = entries[:limit]
    # 任务轮询请求：拿任务号反查任务库，补上用户和真实功能
    jobs = _job_users({it["_jid"] for _, it in entries if it["_jid"] is not None})
    for _, it in entries:
        jid = it.pop("_jid")
        if jid in jobs:
            it["user"], func = jobs[jid]
            it["func"] = func + " · 轮询"
        elif jid is not None:
            it["func"] = "任务轮询"
    return entries, message


def request_logs(limit=200, status="", q="", include_noise=False):
    """聚合各 nginx access log 尾部的后端 /api/ 请求日志（最新在前）。"""
    limit = max(1, min(int(limit or 200), 500))
    entries, message = _collect_request_entries(limit, str(status or "").strip(), str(q or "").strip(), include_noise)
    out = {"items": [item for _, item in entries], "limit": limit}
    if message:
        out["message"] = message
    return out


def activity_logs(days=7, limit=200, category="", q="", source="", include_noise=False):
    """任务记录(jobs 库) + HTTP 请求(nginx) 合并成一条时间线，最新在前。

    category: '' | ok | fail | running（统一语义：任务 done/error/排队中 ↔ HTTP <400/>=400）
    source:   '' | job | http
    """
    limit = max(1, min(int(limit or 200), 500))
    q = str(q or "").strip()
    category = str(category or "").strip()
    source = str(source or "").strip()
    merged, message = [], None

    if source in ("", "http") and category != "running":
        # 成功/失败下推到采集层，避免"失败行被截断挤掉"
        entries, message = _collect_request_entries(
            limit, status=category if category in ("ok", "fail") else "", include_noise=include_noise
        )
        for key, it in entries:
            cat = "ok" if it["status"] < 400 else "fail"
            merged.append(
                (
                    key,
                    {
                        "source": "http",
                        "time": it["time"],
                        "user": it["user"],
                        "func": it["func"],
                        "cat": cat,
                        "status_text": str(it["status"]),
                        "duration_sec": None,
                        "cost": None,
                        "path": it["path"],
                        "method": it["method"],
                        "ip": it["ip"],
                        "ua": it["ua"],
                    },
                )
            )

    if source in ("", "job"):
        for j in call_logs(days, limit)["items"]:
            t = time.localtime(j["created_at"]) if j["created_at"] else None
            key = (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec) if t else (0, 0, 0, 0, 0, 0)
            cat = "ok" if j["status"] == "done" else ("fail" if j["status"] == "error" else "running")
            merged.append(
                (
                    key,
                    {
                        "source": "job",
                        "time": "%02d-%02d %02d:%02d:%02d" % key[1:] if t else "-",
                        "user": j["username"],
                        "func": j["func"],
                        "cat": cat,
                        "status_text": j["status"],
                        "duration_sec": j["duration_sec"],
                        "cost": j["cost"],
                        "path": "",
                        "method": "",
                        "ip": "",
                        "ua": "",
                    },
                )
            )

    items = []
    for key, it in sorted(merged, key=lambda x: x[0], reverse=True):
        if category and it["cat"] != category:
            continue
        if q and q not in it["path"] and q not in (it["user"] or "") and q not in (it["func"] or ""):
            continue
        items.append(it)
        if len(items) >= limit:
            break
    out = {"items": items, "limit": limit, "days": days}
    if message and source != "job":
        out["message"] = message
    return out


def probe_service(svc):
    start = time.time()
    out = dict(svc)
    out.pop("health_url", None)
    try:
        req = urllib.request.Request(svc["health_url"])
        with DIRECT_OPENER.open(req, timeout=3) as r:
            raw = r.read(4096)
        latency = int((time.time() - start) * 1000)
        detail = {}
        try:
            detail = json.loads(raw.decode("utf-8"))
        except Exception:
            detail = {"raw": raw.decode("utf-8", "ignore")[:160]}
        out.update({"online": True, "status": "online", "latency_ms": latency, "detail": detail})
    except urllib.error.HTTPError as e:
        out.update(
            {
                "online": False,
                "status": "offline",
                "latency_ms": int((time.time() - start) * 1000),
                "error": "HTTP %s" % e.code,
            }
        )
    except Exception as e:
        out.update(
            {
                "online": False,
                "status": "offline",
                "latency_ms": int((time.time() - start) * 1000),
                "error": str(e)[:180],
            }
        )
    return out


def service_status():
    return [probe_service(svc) for svc in SERVICES]


def load_channels():
    saved = {}
    with closing(db()) as c:
        rows = c.execute("SELECT * FROM admin_channel_config").fetchall()
    for row in rows:
        try:
            config = json.loads(row["config"] or "{}")
        except Exception:
            config = {}
        saved[row["channel"]] = {
            "enabled": bool(row["enabled"]),
            "config": config,
            "updated_by": row["updated_by"],
            "updated_at": row["updated_at"],
        }
    keys = {item["key"]: item for item in key_status()}
    items = []
    for channel, meta in CHANNELS.items():
        item = saved.get(channel, {})
        items.append(
            {
                "key": channel,
                "name": meta["name"],
                "enabled": bool(item.get("enabled", True)),
                "config": item.get("config") or meta["default_config"],
                "configured": bool(keys.get(channel, {}).get("configured")),
                "updated_by": item.get("updated_by"),
                "updated_at": item.get("updated_at"),
            }
        )
    return items


def load_features(services=None):
    if feature_flags is None:
        return []
    return feature_flags.list_features(services or service_status())


def _validate_config(value, prefix="config"):
    if not isinstance(value, dict):
        raise ValueError("config must be an object")
    clean = {}
    for key, val in value.items():
        key = str(key).strip()
        if not key:
            continue
        if SECRET_RE.search(key):
            raise ValueError("%s.%s cannot contain secret fields" % (prefix, key))
        if isinstance(val, dict):
            clean[key] = _validate_config(val, "%s.%s" % (prefix, key))
        elif isinstance(val, (str, int, float, bool)) or val is None:
            clean[key] = val
        else:
            raise ValueError("%s.%s must be scalar or object" % (prefix, key))
    return clean


def save_channel(actor, body):
    channel = str(body.get("channel") or body.get("key") or "").strip()
    if channel not in CHANNELS:
        raise ValueError("unknown channel")
    enabled = bool(body.get("enabled"))
    config = _validate_config(body.get("config") or {})
    reason = str(body.get("reason") or "").strip()[:200]
    now = int(time.time())
    detail = {"enabled": enabled, "config": config, "reason": reason}
    with closing(db()) as c:
        c.execute(
            """INSERT INTO admin_channel_config(channel, enabled, config, updated_by, updated_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(channel) DO UPDATE SET
                   enabled=excluded.enabled,
                   config=excluded.config,
                   updated_by=excluded.updated_by,
                   updated_at=excluded.updated_at""",
            (channel, 1 if enabled else 0, json.dumps(config, ensure_ascii=False), actor, now),
        )
        c.execute(
            "INSERT INTO admin_audit(actor, action, target, detail, created_at) VALUES(?,?,?,?,?)",
            (actor, "channel.save", channel, json.dumps(detail, ensure_ascii=False), now),
        )
        c.commit()
    return next(item for item in load_channels() if item["key"] == channel)


def save_feature(actor, body):
    if feature_flags is None:
        raise RuntimeError("feature flags unavailable")
    feature = str(body.get("feature") or body.get("key") or "").strip()
    enabled = bool(body.get("enabled"))
    reason = str(body.get("reason") or "").strip()[:200]
    item = feature_flags.set_enabled(feature, enabled, actor)
    now = int(time.time())
    detail = {"enabled": enabled, "reason": reason}
    with closing(db()) as c:
        c.execute(
            "INSERT INTO admin_audit(actor, action, target, detail, created_at) VALUES(?,?,?,?,?)",
            (actor, "feature.toggle", feature, json.dumps(detail, ensure_ascii=False), now),
        )
        c.commit()
    return item


def _empty_stats(message=None):
    return {
        "days": 7,
        "total": 0,
        "by_kind": [],
        "trend": [],
        "high_failure": [],
        "message": message,
    }


def job_stats(days=7):
    days = max(1, min(int(days or 7), 90))
    if not JOB_DB.exists():
        data = _empty_stats("content_jobs.db not found")
        data["days"] = days
        return data
    since = int(time.time()) - days * 86400
    with closing(sqlite3.connect(str(JOB_DB), timeout=10)) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            """SELECT kind, status, COUNT(*) AS n
               FROM jobs WHERE created_at >= ?
               GROUP BY kind, status ORDER BY kind, status""",
            (since,),
        ).fetchall()
        trend_rows = c.execute(
            """SELECT date(created_at, 'unixepoch') AS day, kind, status, COUNT(*) AS n
               FROM jobs WHERE created_at >= ?
               GROUP BY day, kind, status ORDER BY day, kind""",
            (since,),
        ).fetchall()
    by_kind = {}
    total = 0
    for row in rows:
        kind = row["kind"] or "unknown"
        status = row["status"] or "unknown"
        count = int(row["n"] or 0)
        total += count
        bucket = by_kind.setdefault(kind, {"kind": kind, "total": 0, "done": 0, "error": 0, "running": 0, "other": 0})
        bucket["total"] += count
        if status == "done":
            bucket["done"] += count
        elif status == "error":
            bucket["error"] += count
        elif status in {"queued", "running"}:
            bucket["running"] += count
        else:
            bucket["other"] += count
    items = []
    high_failure = []
    for item in by_kind.values():
        success_rate = item["done"] / item["total"] if item["total"] else 0
        failure_rate = item["error"] / item["total"] if item["total"] else 0
        item["success_rate"] = round(success_rate, 4)
        item["failure_rate"] = round(failure_rate, 4)
        items.append(item)
        if item["total"] >= 3 and failure_rate >= 0.5:
            high_failure.append(item)
    trend = [
        {"day": row["day"], "kind": row["kind"], "status": row["status"], "count": int(row["n"] or 0)}
        for row in trend_rows
    ]
    return {
        "days": days,
        "total": total,
        "by_kind": sorted(items, key=lambda x: x["total"], reverse=True),
        "trend": trend,
        "high_failure": sorted(high_failure, key=lambda x: x["failure_rate"], reverse=True),
    }


_PAYLOAD_FIELD_RE = re.compile(r'"(model|provider|mode|keyword|url|line)"\s*:\s*"([^"]*)"')


def _job_payload(raw):
    """payload 只取了前 4KB（整条可达几百 KB，含 base64 图）。截断导致
    JSON 解析失败时，用正则从前缀里捞出功能命名需要的几个小字段。"""
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return dict(_PAYLOAD_FIELD_RE.findall(raw or ""))


# 功能名映射已抽到 func_names —— 原来这里有一份拷贝，和 points._history_func_name 各自漂移了：
# 动作模仿被贴上早已删除的「线路一(HeyGen)」（它现在只走 WaveSpeed），Seedream/果肉生图分不出
# 引擎，果肉/豆姐/欧米三个渠道混成一个「视频 · 小乐」，cinematic/avatar 直接原样吐英文 kind。
call_func_name = func_names.func_name


def call_logs(days=7, limit=200):
    days = max(1, min(int(days or 7), 90))
    limit = max(1, min(int(limit or 200), 500))
    if not JOB_DB.exists():
        return {"days": days, "limit": limit, "items": [], "message": "content_jobs.db not found"}
    since = int(time.time()) - days * 86400
    with closing(sqlite3.connect(str(JOB_DB), timeout=10)) as c:
        c.row_factory = sqlite3.Row
        # substr: payload 整条可达几百 KB(含 base64 图),只取识别功能名所需的前缀。
        # 依赖 jobs(created_at) 索引(idx_jobs_created,2026-07-09 已建),否则 310MB 全表扫要 2 秒
        rows = c.execute(
            """SELECT id, username, kind, cost, status,
                      substr(payload, 1, 4096) AS payload, created_at, updated_at
               FROM jobs
               WHERE created_at >= ?
               ORDER BY created_at DESC, id DESC
               LIMIT ?""",
            (since, limit),
        ).fetchall()
    items = []
    for row in rows:
        created_at = int(row["created_at"] or 0)
        updated_at = int(row["updated_at"] or 0)
        kind = row["kind"] or "unknown"
        payload = _job_payload(row["payload"])
        duration = None
        if created_at and updated_at and updated_at >= created_at:
            duration = updated_at - created_at
        items.append(
            {
                "id": row["id"],
                "username": row["username"] or "-",
                "kind": kind,
                "func": call_func_name(kind, payload),
                "cost": int(row["cost"] or 0),
                "status": row["status"] or "unknown",
                "created_at": created_at,
                "updated_at": updated_at,
                "duration_sec": duration,
            }
        )
    return {"days": days, "limit": limit, "items": items}


class H(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_raw(self, code, body, content_type, disposition=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _token(self):
        return request_token(self.headers)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            raise ValueError("请求体不是合法 JSON")

    def _admin(self):
        user = verify(self._token())
        if not user:
            self._send(401, {"detail": "未登录或登录已过期"})
            return None
        if user.get("role") != "admin":
            self._send(403, {"detail": "需要管理员权限"})
            return None
        return user

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/admin/"):
            return self._send(404, {"detail": "not found"})
        user = self._admin()
        if not user:
            return
        if path == "/api/admin/health":
            return self._send(200, {"ok": True, "service": "huangque-admin"})
        if path == "/api/admin/services":
            return self._send(200, {"items": service_status()})
        if path == "/api/admin/keys":
            return self._send(200, {"items": key_status()})
        if path == "/api/admin/channels":
            return self._send(200, {"items": load_channels()})
        if path == "/api/admin/features":
            return self._send(200, {"items": load_features()})
        if path == "/api/admin/users":
            q = urllib.parse.urlparse(self.path).query
            suffix = "/api/auth/admin/users" + (("?" + q) if q else "")
            try:
                return self._send(200, auth_admin_request(suffix, self._token()))
            except Exception as e:
                return auth_error_response(self, e)
        if path == "/api/admin/points/audit":
            q = urllib.parse.urlparse(self.path).query
            suffix = "/api/auth/admin/points/audit" + (("?" + q) if q else "")
            try:
                return self._send(200, auth_admin_request(suffix, self._token()))
            except Exception as e:
                return auth_error_response(self, e)
        if path == "/api/admin/recharge/orders":
            q = urllib.parse.urlparse(self.path).query
            suffix = "/api/auth/admin/recharge/orders" + (("?" + q) if q else "")
            try:
                return self._send(200, auth_admin_request(suffix, self._token()))
            except Exception as e:
                return auth_error_response(self, e)
        if path in {
            "/api/admin/invite/config", "/api/admin/invite/stats",
            "/api/admin/invite/relations", "/api/admin/invite/audit",
        }:
            q = urllib.parse.urlparse(self.path).query
            suffix = path.replace("/api/admin/", "/api/auth/admin/", 1) + (("?" + q) if q else "")
            try:
                return self._send(200, auth_admin_request(suffix, self._token()))
            except Exception as e:
                return auth_error_response(self, e)
        if path == "/api/admin/invite/export.xlsx":
            q = urllib.parse.urlparse(self.path).query
            suffix = path.replace("/api/admin/", "/api/auth/admin/", 1) + (("?" + q) if q else "")
            try:
                body, content_type, disposition = auth_admin_raw(suffix, self._token())
                return self._send_raw(200, body, content_type, disposition)
            except Exception as e:
                return auth_error_response(self, e)
        if path == "/api/admin/ping":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            svc_key = (q.get("service") or [""])[0].strip()
            key_name = (q.get("key") or [""])[0].strip()
            if svc_key:
                svc = next((s for s in SERVICES if s["key"] == svc_key), None)
                if not svc:
                    return self._send(404, {"detail": "unknown service"})
                return self._send(200, probe_service(svc))
            if key_name:
                fn = KEY_PINGS.get(key_name)
                if not fn:
                    return self._send(400, {"detail": "该密钥不支持在线测试"})
                return self._send(200, fn())
            return self._send(400, {"detail": "需要 service 或 key 参数"})
        if path == "/api/admin/request-logs":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                return self._send(
                    200,
                    request_logs(
                        (q.get("limit") or ["200"])[0],
                        (q.get("status") or [""])[0],
                        (q.get("q") or [""])[0],
                        (q.get("noise") or ["0"])[0] in ("1", "true"),
                    ),
                )
            except Exception as e:
                return self._send(500, {"detail": str(e)[:160]})
        if path == "/api/admin/activity":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                return self._send(
                    200,
                    activity_logs(
                        (q.get("days") or ["7"])[0],
                        (q.get("limit") or ["200"])[0],
                        (q.get("status") or [""])[0],
                        (q.get("q") or [""])[0],
                        (q.get("source") or [""])[0],
                        (q.get("noise") or ["0"])[0] in ("1", "true"),
                    ),
                )
            except Exception as e:
                return self._send(500, {"detail": str(e)[:160]})
        if path == "/api/admin/stats":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(200, job_stats((q.get("days") or ["7"])[0]))
        if path == "/api/admin/call-logs":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(
                200,
                call_logs(
                    (q.get("days") or ["7"])[0],
                    (q.get("limit") or ["200"])[0],
                ),
            )
        if path == "/api/admin/overview":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            services = service_status()
            days = (q.get("days") or ["7"])[0]
            return self._send(
                200,
                {
                    "ok": True,
                    "user": {"username": user.get("username"), "name": user.get("name"), "role": user.get("role")},
                    "services": services,
                    "keys": key_status(),
                    "channels": load_channels(),
                    "features": load_features(services),
                    "stats": job_stats(days),
                },
            )
        return self._send(404, {"detail": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/admin/"):
            return self._send(404, {"detail": "not found"})
        user = self._admin()
        if not user:
            return
        if path == "/api/admin/channel":
            try:
                item = save_channel(user.get("username") or "admin", self._body())
            except ValueError as e:
                return self._send(400, {"detail": str(e)})
            except Exception:
                return self._send(500, {"detail": "保存失败"})
            return self._send(200, {"ok": True, "channel": item})
        if path == "/api/admin/features/toggle":
            try:
                item = save_feature(user.get("username") or "admin", self._body())
            except ValueError as e:
                return self._send(400, {"detail": str(e)})
            except Exception as e:
                return self._send(500, {"detail": str(e)[:160] or "保存失败"})
            return self._send(200, {"ok": True, "feature": item})
        if path == "/api/admin/points/adjust":
            try:
                return self._send(
                    200,
                    auth_admin_request("/api/auth/admin/points/adjust", self._token(), method="POST", payload=self._body()),
                )
            except ValueError as e:
                return self._send(400, {"detail": str(e)})
            except Exception as e:
                return auth_error_response(self, e)
        if path == "/api/admin/membership/set":
            try:
                return self._send(
                    200,
                    auth_admin_request(
                        "/api/auth/admin/membership/set", self._token(), method="POST", payload=self._body(),
                    ),
                )
            except ValueError as e:
                return self._send(400, {"detail": str(e)})
            except Exception as e:
                return auth_error_response(self, e)
        if path == "/api/admin/recharge/review":
            try:
                return self._send(
                    200,
                    auth_admin_request("/api/auth/admin/recharge/review", self._token(), method="POST", payload=self._body()),
                )
            except ValueError as e:
                return self._send(400, {"detail": str(e)})
            except Exception as e:
                return auth_error_response(self, e)
        if path.startswith("/api/admin/invite/relations/"):
            suffix = path.replace("/api/admin/", "/api/auth/admin/", 1)
            try:
                return self._send(200, auth_admin_request(
                    suffix, self._token(), method="POST", payload=self._body(),
                ))
            except ValueError as e:
                return self._send(400, {"detail": str(e)})
            except Exception as e:
                return auth_error_response(self, e)
        return self._send(404, {"detail": "not found"})

    def do_PUT(self):
        path = self.path.split("?", 1)[0]
        if path != "/api/admin/invite/config":
            return self._send(404, {"detail": "not found"})
        user = self._admin()
        if not user:
            return
        try:
            return self._send(200, auth_admin_request(
                "/api/auth/admin/invite/config", self._token(), method="PUT", payload=self._body(),
            ))
        except ValueError as e:
            return self._send(400, {"detail": str(e)})
        except Exception as e:
            return auth_error_response(self, e)

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()


if __name__ == "__main__":
    init_db()
    print("huangque-admin on 127.0.0.1:%d" % PORT)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
