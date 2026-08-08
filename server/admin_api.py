#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Huangque operations admin API.

Stage 1 covers service/key/channel visibility and read-only job statistics.
Admin routes require an admin token; the two explicitly named public inspiration
read/event routes are consumed by the public gallery.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import closing
from http import cookies
from importlib import import_module
import base64
import hashlib
import json
import mimetypes
import os
import pathlib
import re
import subprocess
import threading
import uuid

try:
    import func_names                    # 生产：admin_api.py 直接跑，同目录下就是 func_names.py
    import inspiration_cases
except ModuleNotFoundError:              # 测试：以包的形式 import server.admin_api，server/ 不在 sys.path 上
    from . import func_names
    from . import inspiration_cases
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

_DOMAIN_PACKAGE = (
    __package__ + ".content_domains" if __package__ else "content_domains"
)
egress = import_module(_DOMAIN_PACKAGE + ".egress")
feature_flags = import_module(_DOMAIN_PACKAGE + ".feature_flags")
function_registry = import_module(_DOMAIN_PACKAGE + ".function_registry")
provider_keys = import_module(_DOMAIN_PACKAGE + ".provider_keys")
pricing = import_module(_DOMAIN_PACKAGE + ".pricing")
error_contract = import_module(_DOMAIN_PACKAGE + ".error_contract")


def _optional_content_domain(name):
    try:
        return import_module(_DOMAIN_PACKAGE + "." + name)
    except ImportError:
        return None


points_domain = _optional_content_domain("points")
short_drama_lipsync_diagnostics = _optional_content_domain(
    "short_drama_lipsync_diagnostics"
)
short_drama_lipsync_jobs = _optional_content_domain(
    "short_drama_lipsync_jobs"
)
short_drama_lipsync_observability = _optional_content_domain(
    "short_drama_lipsync_observability"
)
short_drama_lipsync_reconcile = _optional_content_domain(
    "short_drama_lipsync_reconcile"
)
short_drama_lipsync_rollout = _optional_content_domain(
    "short_drama_lipsync_rollout"
)

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
CONTENT_BASE = os.environ.get("CONTENT_BASE", "http://127.0.0.1:8096").rstrip("/")
AUTH_INTERNAL_TOKEN = os.environ.get("HQ_INTERNAL_TOKEN", "")
JOB_DB = pathlib.Path(os.environ.get("CONTENT_JOB_DB", str(BASE / "content_jobs.db")))
ASSET_DB = pathlib.Path(os.environ.get("AUDIO_DB", str(BASE / "audio_assets.db")))
VIDEO_COMPOSE_DB = pathlib.Path(os.environ.get("VIDEO_COMPOSE_DB", str(BASE / "video_compose.db")))
ADMIN_DB = pathlib.Path(os.environ.get("ADMIN_DB", str(BASE / "admin_config.db")))
QA_FIXTURE_DIR = pathlib.Path(os.environ.get("HQ_QA_FIXTURE_DIR", str(BASE / "qa_fixtures")))
CONTENT_OUT = pathlib.Path(os.environ.get("CONTENT_OUT", str(BASE / "content_out")))
E2E_TEST_USERNAME = os.environ.get("HQ_E2E_TEST_USERNAME", "").strip()
E2E_RUN_LOCK = threading.Lock()
E2E_ACTIVE_STATUSES = {"planned", "submitting", "queued", "running", "unknown"}
E2E_BATCH_DEADLINE_SECONDS = 2 * 60 * 60

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

# 服务器实际在用的全部外部 API。
# 名称按真实 API 提供方统一；features 负责映射用户在前端看到的功能名。
KEY_GROUPS = [
    {"key": "xai", "name": "xAI API", "category": "视频生成",
     "features": ["视频模块 → 果肉视频生成"], "env_features": [],
     "pool_features": ["视频模块 → 果肉视频生成"],
     "pool_base_env": ["XAI_API_BASE"], "pool_base_default": "https://api.x.ai/v1",
     "env": ["XAI_API_KEY"], "pool_provider": "xai"},
    {"key": "openai", "name": "OpenAI API", "category": "图片生成 / 视频生成",
     "features": ["图片生成 → 黄雀引擎 2", "视频模块 → Sora 2"],
     "env_features": ["图片生成 → 黄雀引擎 2"], "pool_features": ["视频模块 → Sora 2"],
     "env_base_env": ["OPENAI_OFFICIAL_BASE"], "env_base_default": "https://api.openai.com",
     "pool_base_env": ["OPENAI_BASE"], "pool_base_default": "https://api.openai.com",
     "env": ["OPENAI_API_KEY"], "pool_provider": "sora"},
    {"key": "gemini", "name": "Google Gemini API", "category": "图片生成 / 视频生成",
     "features": ["图片生成 → 纳米香蕉", "视频模块 → Omni 视频"],
     "env_features": ["图片生成 → 纳米香蕉"], "pool_features": ["视频模块 → Omni 视频"],
     "env_base_env": ["GEMINI_OFFICIAL_BASE"], "env_base_default": "https://generativelanguage.googleapis.com",
     "pool_base_env": ["GEMINI_OMNI_BASE", "GEMINI_BASE"], "pool_base_default": "https://generativelanguage.googleapis.com",
     "env": ["GEMINI_API_KEY"], "pool_provider": "omni"},
    {"key": "seedance", "name": "火山方舟 API", "category": "图片生成 / 视频生成",
     "features": ["图片生成 → 黄雀引擎 1（Seedream）", "视频模块 → Seedance 视频"],
     "env_features": ["图片生成 → 黄雀引擎 1（Seedream）"], "pool_features": ["视频模块 → Seedance 视频"],
     "env_base_env": ["ARK_BASE"], "env_base_default": "https://ark.cn-beijing.volces.com/api/v3",
     "pool_base_env": ["ARK_BASE"], "pool_base_default": "https://ark.cn-beijing.volces.com/api/v3",
     "env": ["ARK_API_KEY"], "pool_provider": "seedance"},
    {"key": "minimax", "name": "MiniMax 中国区 API", "category": "视频生成",
     "features": ["视频模块 → 麦克视频"], "env_features": [],
     "pool_features": ["视频模块 → 麦克视频"],
     "pool_base_env": ["MINIMAX_API_BASE"], "pool_base_default": "https://api.minimaxi.com",
     "env": ["MINIMAX_API_KEY"], "pool_provider": "minimax"},
    {"key": "zelong", "name": "小乐 AI API", "category": "图片生成",
     "features": ["图片生成 → 黄雀引擎 2 备用线路"], "env": ["ZELONG_KEY"]},
    {"key": "zelong2", "name": "泽龙 API", "category": "图片生成",
     "features": ["图片生成 → 泽龙 2 备用线路（维护中）"], "env": ["ZELONG2_KEY"]},
    {"key": "heygen", "name": "HeyGen API", "category": "数字化 IP / 视频生成",
     "features": ["视频模块 → 电影化身", "视频模块 → 数字人口播", "我的资产 → 数字人形象"],
     "env_base_env": ["HEYGEN_API_BASE"], "env_base_default": "https://api.heygen.com/v3",
     "env": ["HEYGEN_API_KEY"]},
    {"key": "heygen_relay", "name": "HeyGen 中转 API", "category": "数字化 IP / 视频生成",
     "features": ["数字化 IP → 中转与下载兜底"],
     "env_base_env": ["HEYGEN_RELAY_BASE"], "env_base_default": "",
     "env": ["HEYGEN_RELAY_TOKEN"]},
    {"key": "xiaolevideo", "name": "小乐视频 API", "category": "图片生成 / 视频生成",
     "features": ["图片生成 → 果肉生图", "视频模块 → 历史兼容线路"], "env": ["XIAOLEVIDEO_API_KEY"]},
    {"key": "runninghub", "name": "RunningHub API", "category": "视频处理",
     "features": ["视频模块 → 换装换背景 · 线路一"], "env": ["RUNNINGHUB_API_KEY", "RUNNINGHUB_KEY"]},
    {"key": "wavespeed", "name": "WaveSpeed API", "category": "视频处理",
     "features": ["视频模块 → 换装换背景 · 线路二", "视频模块 → Seedance AI 超清"], "env": ["WAVESPEED_API_KEY"]},
    {"key": "cosyvoice", "name": "阿里百炼 API", "category": "音频生成",
     "features": ["AI 配音 → 公共音色", "AI 配音 → 声音克隆"], "env": ["DASHSCOPE_API_KEY"]},
    {"key": "tikhub", "name": "TikHub API", "category": "内容采集 / 获客",
     "features": ["内容采集 → 抖音 / 小红书 / 视频号", "获客分析 → 评论与线索"], "env": ["TIKHUB_KEY", "TIKHUB_API_KEY"]},
    {"key": "cos", "name": "腾讯云 COS", "category": "基础设施",
     "features": ["我的资产 → 生成结果存储", "视频模块 → 参考素材与成片存储"], "env": ["COS_SECRET_ID", "COS_SECRET_KEY", "COS_REGION", "COS_BUCKET"]},
]
KEY_GROUP_MAP = {item["key"]: item for item in KEY_GROUPS}
_CREDENTIAL_VERSION_SALT = os.urandom(16)
_PROBE_CONFIG_ENVS = {
    "openai": ["OPENAI_BASE"],
    "xai": ["XAI_API_BASE"],
    "gemini": ["GEMINI_BASE"],
    "heygen": ["HEYGEN_MCP_CREDENTIALS"],
    "tikhub": ["TIKHUB_BASE"],
    "heygen_relay": ["HEYGEN_RELAY_BASE"],
    "xiaolevideo": ["XIAOLEVIDEO_API_BASE"],
    "cos": ["COS_DOMAIN"],
}

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
   "d": "口播直连：上传已合成的 mp3 音频换 asset_id",
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
 "cosyvoice": [
  {
   "m": "WS",
   "p": "/api-ws/v1/inference",
   "d": "CosyVoice 公共及个人音色语音合成",
   "fee": true
  },
  {
   "m": "POST",
   "p": "/api/v1/services/audio/tts/customization",
   "d": "CosyVoice 创建、查询和删除复刻音色",
   "fee": true
  }
 ]
}
""")
ENDPOINT_CATALOG["xai"] = [
    {"m": "POST", "p": "/v1/videos/generations", "d": "果肉视频生成", "fee": True},
    {"m": "POST", "p": "/v1/videos/edits", "d": "果肉视频编辑", "fee": True},
    {"m": "GET", "p": "/v1/videos/{request_id}", "d": "查询果肉视频状态", "fee": False},
]

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
HERMES_AUDIT_LOGS = [
    pathlib.Path(p.strip())
    for p in os.environ.get(
        "HERMES_AUDIT_LOGS",
        "/home/ubuntu/hermes-web/data/audit/security.jsonl",
    ).split(",")
    if p.strip()
]
# nginx combined 格式：ip - user [time] "METHOD path HTTP/x" status size "referer" "ua"
# remote_user 可能带空格（basic auth），所以 ip 之后宽松匹配到第一个 [
LOG_LINE_RE = re.compile(
    r'^(?P<ip>\S+) [^\[]*\[(?P<time>[^\]]+)\] "(?P<method>[A-Z]+) (?P<path>\S+)[^"]*" '
    r'(?P<status>\d{3}) (?P<size>\d+|-) "[^"]*" "(?P<ua>[^"]*)"'
)
LOG_META_RE = re.compile(
    r"\srt=(?P<duration>[0-9]+(?:\.[0-9]+)?)\srid=(?P<request_id>[A-Za-z0-9_-]+)"
    r"(?:\shq=(?P<hq_code>[A-Z0-9-]+|-))?\s*$"
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


def _xai_proxy_url():
    """Use the same egress route as paid xAI video requests."""
    return egress.preferred_proxy(PROXY_URL) if egress is not None else PROXY_URL


def _heygen_proxy_url():
    """Use the same dedicated egress route as direct HeyGen video requests."""
    return egress.heygen_proxy() if egress is not None else PROXY_URL


def db():
    ADMIN_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(ADMIN_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def lipsync_db():
    JOB_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(JOB_DB), timeout=10)
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
        c.execute(
            """CREATE TABLE IF NOT EXISTS admin_e2e_runs(
                run_id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL DEFAULT '',
                operation_id TEXT NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                job_id INTEGER,
                cost INTEGER NOT NULL DEFAULT 0,
                points_before INTEGER,
                points_after INTEGER,
                transaction_key TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )"""
        )
        columns = {row[1] for row in c.execute("PRAGMA table_info(admin_e2e_runs)")}
        if "batch_id" not in columns:
            c.execute("ALTER TABLE admin_e2e_runs ADD COLUMN batch_id TEXT NOT NULL DEFAULT ''")
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_e2e_operation ON admin_e2e_runs(operation_id, created_at DESC)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_e2e_batch ON admin_e2e_runs(batch_id, created_at)"
        )
        # ponytail: the first batch queue is process-local; on an Admin restart, fail only
        # never-submitted rows. Move orchestration to a durable worker if restarts become common.
        c.execute(
            """UPDATE admin_e2e_runs SET status='failed',error=?,updated_at=?
               WHERE batch_id<>'' AND status='planned'""",
            ("后台重启，尚未提交的批次项目已安全停止，未扣点", int(time.time())),
        )
        c.execute(
            """UPDATE admin_e2e_runs SET status='unknown',error=?,updated_at=?
               WHERE batch_id<>'' AND status='submitting' AND job_id IS NULL""",
            ("后台在提交阶段重启，结果未知；禁止自动重试", int(time.time())),
        )
        c.commit()
    if feature_flags is not None:
        feature_flags.init_db()
    pricing.init_db()
    if provider_keys is not None:
        provider_keys.init_db()
    if short_drama_lipsync_rollout is not None:
        short_drama_lipsync_rollout.init_db(lipsync_db)
    if short_drama_lipsync_observability is not None:
        short_drama_lipsync_observability.init_db(lipsync_db)


    inspiration_cases.init_db(ADMIN_DB)


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


def _fixture_data_url(value):
    prefix = "@fixture/"
    if not isinstance(value, str) or not value.startswith(prefix):
        return value
    name = value[len(prefix):]
    if not name or pathlib.Path(name).name != name:
        raise ValueError("测试素材名称无效")
    path = (QA_FIXTURE_DIR / name).resolve()
    if path.parent != QA_FIXTURE_DIR.resolve() or not path.is_file():
        raise ValueError("测试素材未部署：" + name)
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return "data:%s;base64,%s" % (
        mime, base64.b64encode(path.read_bytes()).decode("ascii")
    )


def _e2e_payload(operation_id, runner, ready_avatar_ids=None):
    prefill = runner.get("prefill") or {}
    resolve = lambda value: _fixture_data_url(value)
    prompt = str(prefill.get("prompt") or "").strip()
    payload = {"qa_operation_id": operation_id}
    if operation_id == "video.digital_ip.text.single":
        payload.update({
            "mode": "text", "image_data": resolve(prefill["image_url"]),
            "text": prompt, "voice": "S_d21F8OR62", "resolution": "720p",
            "ratio": "9:16", "motion": "medium", "subtitle": False,
            "bgm_data": "", "bgm_volume": 0.18,
        })
    elif operation_id == "video.digital_ip.audio":
        payload.update({
            "mode": "audio", "image_data": resolve(prefill["image_url"]),
            "audio_data": resolve(prefill["audio_url"]), "resolution": "720p",
            "ratio": "9:16", "motion": "medium", "subtitle": False,
            "bgm_data": "", "bgm_volume": 0.18,
        })
    elif operation_id.startswith("video.cinematic."):
        avatar_ids = [int(item) for item in (ready_avatar_ids or []) if item]
        if not avatar_ids:
            raise ValueError("专用测试账号尚未登记已就绪电影化身形象")
        if operation_id == "video.cinematic.motion":
            payload.update({
                "cine_mode": "motion", "avatar_ids": avatar_ids[:1],
                "reference_video_data": resolve(prefill["reference_video_url"]),
                "ratio": "9:16",
            })
        else:
            payload.update({
                "cine_mode": "open", "avatar_ids": avatar_ids[:1],
                "prompt": prompt, "duration": 4, "resolution": "720p",
                "ratio": "9:16", "enhance_prompt": False,
            })
    elif operation_id == "video.tryon.fast":
        payload.update({
            "line": "2", "person_image_data": resolve(prefill["reference_video_url"]),
            "clothes_data": resolve(prefill["image_url"]), "seconds": 5,
        })
    elif operation_id == "video.tryon.classic":
        payload.update({
            "line": "1", "person_video_data": resolve(prefill["reference_video_url"]),
            "clothes_data": resolve(prefill["image_url"]),
            "background_data": resolve(prefill["background_url"]), "seconds": 5,
        })
    elif operation_id.startswith("video.sora."):
        payload.update({
            "mode": "sora", "prompt": prompt, "model": "sora-2", "seconds": 4,
            "resolution": "720p", "ratio": "9:16",
            "reference_images": [resolve(item) for item in prefill.get("reference_images") or []],
        })
    elif operation_id.startswith("video.grok."):
        references = [resolve(item) for item in prefill.get("reference_images") or []]
        payload.update({
            "channel": "grok", "operation": "generate", "model": "grok-imagine-video",
            "prompt": prompt, "duration": 5,
            "resolution": "720p" if references else "480p", "ratio": "9:16",
            "reference_images": references,
        })
    elif operation_id.startswith("video.minimax."):
        payload.update({
            "channel": "minimax", "operation": "generate", "prompt": prompt,
            "duration": 5, "resolution": "768p", "ratio": "9:16",
            "reference_images": [resolve(item) for item in prefill.get("reference_images") or []],
        })
    elif operation_id.startswith("video.omni."):
        payload.update({
            "channel": "omni", "operation": "generate", "prompt": prompt,
            "duration": 4, "resolution": "720p", "ratio": "9:16",
            "reference_images": [resolve(item) for item in prefill.get("reference_images") or []],
        })
    elif operation_id.startswith("video.seedance."):
        payload.update({
            "channel": "micro", "operation": "generate", "prompt": prompt,
            "duration": 4, "resolution": "480p", "ratio": "9:16",
            "generate_audio": False, "upscale": False,
            "reference_images": [resolve(item) for item in prefill.get("reference_images") or []],
        })
    else:
        raise ValueError("该模式尚未接入后台托管测试")
    return payload


def _content_e2e_request(path, account_token, payload, idempotency_key, expected_cost):
    headers = {
        "Authorization": "Bearer " + account_token,
        "Content-Type": "application/json; charset=utf-8",
        "Idempotency-Key": idempotency_key,
        "X-HQ-Internal-Token": AUTH_INTERNAL_TOKEN,
        "X-HQ-Expected-Cost": str(int(expected_cost)),
    }
    req = urllib.request.Request(
        CONTENT_BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=35) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"{}")
        except Exception:
            body = {}
        err = RuntimeError(body.get("detail") or "业务接口拒绝测试任务")
        err.status = exc.code
        err.body = body
        raise err


def _content_e2e_get(path, account_token):
    req = urllib.request.Request(
        CONTENT_BASE + path,
        headers={"Authorization": "Bearer " + account_token},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"{}")
        except Exception:
            body = {}
        raise RuntimeError(body.get("detail") or "读取测试账号素材失败")
    except urllib.error.URLError as exc:
        raise RuntimeError("读取测试账号素材失败：" + str(exc.reason)[:140])


def _content_e2e_post(path, account_token, payload):
    request = urllib.request.Request(
        CONTENT_BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + account_token,
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"{}")
        except Exception:
            body = {}
        raise ValueError(body.get("detail") or "测试包参数校验失败")


def _e2e_kind(endpoint):
    return {
        "/api/gen/video": "video", "/api/gen/tryon": "tryon",
        "/api/gen/xiaole_video": "xiaole_video", "/api/gen/sora_video": "sora_video",
        "/api/gen/cinematic": "cinematic",
    }.get(endpoint)


def _ready_avatar_ids(operation_id, account_token):
    if not operation_id.startswith("video.cinematic."):
        return []
    avatar_data = _content_e2e_get("/api/gen/video/avatars?limit=120", account_token)
    return [
        item.get("id") for item in avatar_data.get("items") or []
        if item.get("status") == "ready" and item.get("id")
    ]


def _e2e_cost(operation_id, kind, payload, account_token):
    if operation_id == "video.cinematic.motion":
        return int(_content_e2e_post("/api/gen/cinematic/quote", account_token, payload)["cost"])
    return int(points_domain.cost_of(kind, dict(payload)))


def _e2e_parameters(operation_id, payload):
    values = {
        "cine_mode": {"motion": "动作模仿", "open": "开放式生成"}.get(
            payload.get("cine_mode"), payload.get("cine_mode")
        ),
        "duration": "随参考视频" if operation_id == "video.cinematic.motion"
        else payload.get("duration") or payload.get("seconds"),
        "resolution": payload.get("resolution"), "ratio": payload.get("ratio"),
        "generate_audio": "开启" if payload.get("generate_audio") else (
            "关闭" if "generate_audio" in payload else None
        ),
    }
    labels = {
        "cine_mode": "模式", "duration": "时长", "resolution": "清晰度",
        "ratio": "画面比例", "generate_audio": "生成音频",
    }
    parameters = []
    for key in ("cine_mode", "duration", "resolution", "ratio", "generate_audio"):
        if values[key] is not None:
            suffix = " 秒" if key == "duration" and isinstance(values[key], int) else ""
            parameters.append("%s：%s%s" % (labels[key], values[key], suffix))
    return parameters


def _e2e_prepare_operation(session, operation_id):
    runner = function_registry.e2e_runner(operation_id)
    if not runner:
        raise ValueError("功能注册表中不存在该模式")
    if not runner.get("supported"):
        raise ValueError(runner.get("blocked_reason") or "该模式尚未接入后台托管测试")
    account_token = session["token"]
    avatars = _ready_avatar_ids(operation_id, account_token)
    payload = _e2e_payload(operation_id, runner, avatars)
    endpoint = runner["endpoint"]["path"]
    kind = _e2e_kind(endpoint)
    if not kind:
        raise ValueError("该模式的业务接口尚未接入后台托管测试")
    return {
        "operation_id": operation_id, "runner": runner, "payload": payload,
        "endpoint": endpoint, "kind": kind,
        "cost": _e2e_cost(operation_id, kind, payload, account_token),
        "parameters": _e2e_parameters(operation_id, payload),
    }


def e2e_preflight(admin_token, operation_id):
    """Resolve the private fixture and current quote without creating a paid task."""
    runner = function_registry.e2e_runner(operation_id)
    if not runner:
        raise ValueError("功能注册表中不存在该模式")
    if not runner.get("supported"):
        return {"operation_id": operation_id, "ready": False,
                "blocker": runner.get("blocked_reason") or "测试包尚未准备完成"}
    active = next((run for run in list_e2e_runs(100)
                   if run["status"] in E2E_ACTIVE_STATUSES), None)
    if active:
        return {"operation_id": operation_id, "ready": False,
                "blocker": "另一条生产链测试正在运行，请等待终态后再继续"}
    if not points_domain:
        raise RuntimeError("点数模块不可用")
    session = auth_admin_request("/api/auth/admin/e2e/session", admin_token, method="POST", payload={})
    account = session["account"]
    prepared = _e2e_prepare_operation(session, operation_id)
    cost = prepared["cost"]
    points = int(account.get("points") or 0)
    membership = bool(account.get("membership_active"))
    blocker = ""
    if not membership:
        blocker = "专用测试账号会员未生效"
    elif points < cost:
        blocker = "专用测试账号点数不足：需要 %s 点，当前 %s 点" % (cost, points)
    return {
        "operation_id": operation_id, "ready": not blocker, "blocker": blocker,
        "account": account.get("username") or "专用测试账号", "points": points,
        "cost": cost, "parameters": prepared["parameters"],
        "fixture_ready": True,
        "ready_avatar": True if operation_id.startswith("video.cinematic.") else None,
    }


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


def _key_group_values(item, sources=None):
    sources = env_sources() if sources is None else sources
    found = []
    for env_name in item["env"]:
        for src in sources:
            value = (src["values"].get(env_name) or "").strip()
            if value:
                found.append(
                    {"env": env_name, "source": src["name"], "value": value}
                )
                break
    return found


def _key_group_base_host(item, prefix, sources):
    value = ""
    for env_name in item.get(prefix + "_base_env", []):
        for src in sources:
            value = (src["values"].get(env_name) or "").strip()
            if value:
                break
        if value:
            break
    value = value or str(item.get(prefix + "_base_default") or "").strip()
    if not value:
        return ""
    try:
        parsed = urllib.parse.urlsplit(
            value if "://" in value else "https://" + value
        )
        host = parsed.hostname or ""
        return host + ((":" + str(parsed.port)) if parsed.port else "")
    except (TypeError, ValueError):
        return ""


def _key_group_version(item, sources=None):
    """Opaque per-process version; detects every credential/base change without exposing it."""
    sources = env_sources() if sources is None else sources
    names = list(item.get("env", []))
    names += list(item.get("env_base_env", []))
    names += list(item.get("pool_base_env", []))
    names += list(_PROBE_CONFIG_ENVS.get(item.get("key"), []))
    values = []
    for name in names:
        value = next(
            ((src["values"].get(name) or "").strip() for src in sources
             if (src["values"].get(name) or "").strip()),
            "",
        )
        values.append(name + "=" + value)
        if name == "HEYGEN_MCP_CREDENTIALS" and value:
            try:
                content = pathlib.Path(value).read_bytes()
                values.append("HEYGEN_MCP_FILE=" + hashlib.sha256(_CREDENTIAL_VERSION_SALT + content).hexdigest())
            except OSError:
                values.append("HEYGEN_MCP_FILE=unreadable")
    if not any(value.rsplit("=", 1)[-1] for value in values):
        return ""
    payload = "\0".join(values).encode("utf-8")
    return hashlib.sha256(_CREDENTIAL_VERSION_SALT + payload).hexdigest()[:16]


def _credential_version(key):
    item = KEY_GROUP_MAP.get(str(key or "").strip().lower())
    return _key_group_version(item) if item else ""


def key_status():
    sources = env_sources()
    items = []
    for item in KEY_GROUPS:
        values = _key_group_values(item, sources)
        found = [
            {"env": value["env"], "source": value["source"]}
            for value in values
        ]
        last4 = values[0]["value"][-4:] if values else ""
        configured = len(found) == len(item["env"])
        if item["key"] in {"runninghub", "tikhub", "heygen_relay"}:
            configured = bool(found)
        items.append(
            {
                "key": item["key"],
                "name": item["name"],
                "category": item["category"],
                "features": list(item["features"]),
                "env_features": list(item.get("env_features", item["features"])),
                "pool_features": list(item.get("pool_features", [])),
                "env_base_host": _key_group_base_host(item, "env", sources),
                "pool_base_host": _key_group_base_host(item, "pool", sources),
                "pool_provider": item.get("pool_provider"),
                "configured": configured,
                "required_env": item["env"],
                "sources": found,
                "last4": last4,
                "credential_version": _key_group_version(item, sources),
                "management": "server_env",
                "pingable": item["key"] in KEY_PINGS,
                "auto_probe": item["key"] in AUTO_KEY_PING_INTERVALS,
                "probe_interval": AUTO_KEY_PING_INTERVALS.get(item["key"]),
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


def _heygen_balances(detail):
    details = ((detail or {}).get("data") or {}).get("details") or {}
    try:
        return float(details["plan_credit"]), float(details.get("api") or 0)
    except (KeyError, TypeError, ValueError):
        return None


def _ping_upstream(method, url, headers=None, body=None, proxied=False, timeout=12,
                   proxy_url=None):
    """真实调一次上游 API。只返回状态码/耗时/错误摘要，绝不含密钥。"""
    if proxy_url is None:
        opener = PROXY_OPENER if proxied else DIRECT_OPENER
    elif proxy_url:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        )
    else:
        opener = DIRECT_OPENER
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
                if detail.get("error") or (detail.get("result") or {}).get("isError"):
                    out.update({"ok": False, "error": "上游业务错误"})
                code = detail.get("code")
                if code is not None and str(code) not in ("0", "200"):
                    out["ok"] = False
                    out["error"] = "业务码 %s: %s" % (code, str(detail.get("msg") or detail.get("message") or "")[:120])
                heygen = _heygen_balances(detail)
                if heygen is not None:
                    out["plan_credit"], out["api_wallet"] = heygen
                else:
                    balance = _find_balance(detail)
                    if balance is not None:
                        out["balance"] = balance
        except Exception:
            try:
                messages = [
                    json.loads(line[6:]) for line in raw.decode("utf-8").splitlines()
                    if line.startswith("data: ")
                ]
                message = messages[-1] if messages else {}
                if message.get("error") or (message.get("result") or {}).get("isError"):
                    out.update({"ok": False, "error": "MCP 业务错误"})
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


def _key_ping_xai():
    key = _env_value(["XAI_API_KEY"])
    if not key:
        return {"ok": False, "error": "密钥未配置"}
    base = (_env_value(["XAI_API_BASE"]) or "https://api.x.ai/v1").rstrip("/")
    return _ping_upstream(
        "GET", base + "/models",
        headers={"Authorization": "Bearer " + key},
        proxy_url=_xai_proxy_url() if "api.x.ai" in base else "",
    )


def _key_ping_heygen_mcp():
    value = _env_value(["HEYGEN_MCP_CREDENTIALS"])
    path = pathlib.Path(value) if value else None
    if not path or not path.is_file():
        return {"ok": False, "status": "not_configured", "mode": "auth"}
    try:
        if path.stat().st_mode & 0o077:
            return {"ok": False, "status": "credential_rejected", "mode": "auth"}
        credentials = json.loads(path.read_text(encoding="utf-8"))
        token = str(credentials.get("access_token") or "").strip()
        expires_at = float(credentials.get("expires_at") or 0)
    except Exception:
        return {"ok": False, "status": "credential_rejected", "mode": "auth"}
    if not token:
        return {"ok": False, "status": "credential_rejected", "mode": "auth"}
    if expires_at <= time.time() + 60:
        status = "credential_refresh_pending" if credentials.get("refresh_token") else "credential_rejected"
        return {"ok": False, "status": status, "mode": "auth"}
    return _ping_upstream(
        "POST", "https://mcp.heygen.com/mcp/v1/",
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": "2025-06-18",
        },
        body={
            "jsonrpc": "2.0", "id": "huangque-healthcheck", "method": "tools/call",
            "params": {"name": "get_current_user", "arguments": {}},
        },
        proxy_url=_heygen_proxy_url(),
    )


def _key_ping_heygen():
    key = _env_value(["HEYGEN_API_KEY"])
    if not key:
        return {"ok": False, "error": "密钥未配置"}
    api = _ping_upstream(
        "GET", "https://api.heygen.com/v2/user/remaining_quota",
        headers={"X-Api-Key": key}, proxy_url=_heygen_proxy_url(),
    )
    api["components"] = "API Key"
    if not api.get("ok") or not _env_value(["HEYGEN_MCP_CREDENTIALS"]):
        return api
    mcp = _key_ping_heygen_mcp()
    for field in ("plan_credit", "api_wallet"):
        if api.get(field) is not None:
            mcp[field] = api[field]
    mcp["latency_ms"] = int(api.get("latency_ms") or 0) + int(mcp.get("latency_ms") or 0)
    mcp["components"] = "API Key + MCP OAuth"
    return mcp


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


def _key_ping_seedance():
    key = _env_value(["ARK_API_KEY"])
    if not key:
        return {"ok": False, "error": "密钥未配置", "mode": "auth"}
    return probe_provider_secret("seedance", key)


def _key_ping_minimax():
    key = _env_value(["MINIMAX_API_KEY"])
    if not key:
        return {"ok": False, "error": "密钥未配置", "mode": "auth"}
    return probe_provider_secret("minimax", key)


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


def _key_ping_cosyvoice():
    key = _env_value(["DASHSCOPE_API_KEY"])
    if not key:
        return {"ok": False, "error": "密钥未配置", "mode": "auth"}
    return _ping_upstream(
        "POST",
        "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        body={
            "model": "voice-enrollment",
            "input": {"action": "list_voice", "page_index": 0, "page_size": 1},
        },
        proxied=False,
    )


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
    "xai": _key_ping_xai,
    "openai": _key_ping_openai,
    "gemini": _key_ping_gemini,
    "seedance": _key_ping_seedance,
    "minimax": _key_ping_minimax,
    "zelong": _key_ping_zelong,
    "zelong2": _key_ping_zelong2,
    "heygen": _key_ping_heygen,
    "heygen_relay": _key_ping_heygen_relay,
    "xiaolevideo": _key_ping_xiaolevideo,
    "runninghub": _key_ping_runninghub,
    "wavespeed": _key_ping_wavespeed,
    "cosyvoice": _key_ping_cosyvoice,
    "tikhub": _key_ping_tikhub,
    "cos": _key_ping_cos,
}

# 只自动检查明确的非生成接口；未知合约渠道仍保留管理员手动测试。
AUTO_KEY_PING_INTERVALS = {
    "openai": 600,
    "xai": 600,
    "gemini": 600,
    "heygen": 300,
    "runninghub": 300,
    "wavespeed": 300,
    "tikhub": 300,
    "heygen_relay": 600,
    "xiaolevideo": 600,
    "cos": 600,
    "cosyvoice": 3600,
}
PROVIDER_KEY_PING_INTERVAL = 240
_KEY_PING_CACHE = {}
_KEY_PING_LOCKS = {}
_KEY_PING_GUARD = threading.Lock()
_KEY_PROBE_MONITOR_STARTED_AT = 0
_KEY_PROBE_MONITOR_LAST_CYCLE = 0


def _public_probe(value):
    return {key: val for key, val in value.items() if not key.startswith("_")}


def _probe_status(key, raw):
    mode = str(raw.get("mode") or "auth")
    explicit = str(raw.get("status") or "")
    explicit_messages = {
        "not_configured": "服务器凭据未配置完整",
        "credential_rejected": "凭据已被拒绝",
        "credential_refresh_pending": "MCP OAuth 待刷新验证",
    }
    if explicit in explicit_messages:
        return explicit, explicit_messages[explicit]
    try:
        code = int(raw.get("http_status") or 0)
    except (TypeError, ValueError):
        code = 0
    if raw.get("ok"):
        if mode == "reach":
            return "reachable_only", "仅连通，未验证凭据"
        if key == "heygen" and isinstance(raw.get("plan_credit"), (int, float)) and raw["plan_credit"] <= 0:
            # 当前 HeyGen 客户链路统一优先使用套餐额度；禁止无提示切到高价 API 钱包。
            if isinstance(raw.get("api_wallet"), (int, float)) and raw["api_wallet"] > 0:
                return "quota_or_plan", "套餐额度已空，已阻断高价 API 钱包兜底"
            return "quota_or_plan", "套餐额度已耗尽"
        return "auth_ok", "鉴权通过"
    if code == 401:
        return "credential_rejected", "凭据已被拒绝"
    if code == 402:
        return "quota_or_plan", "额度或套餐不足"
    if code == 403:
        return "permission_denied", "权限或模型未开通"
    if code == 429:
        return "throttled", "渠道限流"
    if code >= 500:
        return "upstream_unavailable", "渠道暂时不可用"
    if code in (400, 404):
        return "probe_contract_error", "检测接口可能已变更"
    error = str(raw.get("error") or "")
    if "未配置" in error or "配置不全" in error:
        return "not_configured", "服务器凭据未配置完整"
    if code:
        return "business_error", "渠道返回业务异常"
    return "network_error", "网络连接失败"


def _probe_cache_fresh(cached, key, now, version):
    if not cached or cached.get("credential_version") != version:
        return False
    ttl = AUTO_KEY_PING_INTERVALS.get(key, 60)
    if cached.get("status") in {"network_error", "upstream_unavailable", "throttled"}:
        ttl = min(ttl, 60)
    return now - float(cached.get("_monotonic_at") or 0) < ttl


def key_probe_status():
    with _KEY_PING_GUARD:
        snapshot = {key: dict(value) for key, value in _KEY_PING_CACHE.items()}
    return {
        key: _public_probe(value)
        for key, value in snapshot.items()
        if value.get("credential_version") == _credential_version(key)
    }


def probe_key(key, force=False):
    key = str(key or "").strip().lower()
    fn = KEY_PINGS.get(key)
    if not fn:
        raise ValueError("该密钥不支持在线测试")
    now = time.monotonic()
    version = _credential_version(key)
    with _KEY_PING_GUARD:
        cached = _KEY_PING_CACHE.get(key)
        if not force and _probe_cache_fresh(cached, key, now, version):
            return dict(_public_probe(cached), cached=True)
        lock = _KEY_PING_LOCKS.setdefault(key, threading.Lock())
    with lock:
        now = time.monotonic()
        version = _credential_version(key)
        with _KEY_PING_GUARD:
            cached = _KEY_PING_CACHE.get(key)
            if not force and _probe_cache_fresh(cached, key, now, version):
                return dict(_public_probe(cached), cached=True)
        try:
            raw = fn() or {}
        except Exception:
            raw = {"ok": False}
        current_version = _credential_version(key)
        if current_version != version:
            return {
                "channel": key, "ok": False, "mode": "auth",
                "status": "credential_changed", "message": "凭据刚刚变更，等待重新检测",
                "checked_at": int(time.time()), "credential_version": current_version,
                "cached": False,
            }
        status, message = _probe_status(key, raw)
        result = {
            "channel": key,
            "ok": status in {"auth_ok", "reachable_only"},
            "mode": str(raw.get("mode") or "auth"),
            "status": status,
            "message": message,
            "checked_at": int(time.time()),
            "credential_version": version,
            "_monotonic_at": time.monotonic(),
        }
        if raw.get("components") in {"API Key", "API Key + MCP OAuth"}:
            result["components"] = raw["components"]
        for field in ("http_status", "latency_ms"):
            try:
                if raw.get(field) is not None:
                    result[field] = int(raw[field])
            except (TypeError, ValueError):
                pass
        if key in {"runninghub", "wavespeed", "tikhub"} and isinstance(raw.get("balance"), (int, float)):
            result["balance"] = raw["balance"]
        if key == "heygen":
            for field in ("plan_credit", "api_wallet"):
                if isinstance(raw.get(field), (int, float)):
                    result[field] = raw[field]
        with _KEY_PING_GUARD:
            _KEY_PING_CACHE[key] = result
        return dict(_public_probe(result), cached=False)


def probe_configured_keys():
    configured = {item["key"] for item in key_status() if item["configured"]}
    for key in AUTO_KEY_PING_INTERVALS:
        if key in configured:
            try:
                probe_key(key)
            except Exception:
                continue


def key_probe_monitor(stop_event):
    global _KEY_PROBE_MONITOR_LAST_CYCLE
    while not stop_event.is_set():
        try:
            probe_configured_keys()
            probe_provider_keys()
        except Exception:
            pass
        _KEY_PROBE_MONITOR_LAST_CYCLE = int(time.time())
        stop_event.wait(30)


def start_key_probe_monitor():
    global _KEY_PROBE_MONITOR_STARTED_AT
    _KEY_PROBE_MONITOR_STARTED_AT = int(time.time())
    thread = threading.Thread(
        target=key_probe_monitor, args=(threading.Event(),),
        daemon=True, name="admin-key-probe-monitor",
    )
    thread.start()
    return thread


def key_probe_monitor_status():
    return {
        "running": bool(_KEY_PROBE_MONITOR_STARTED_AT),
        "started_at": _KEY_PROBE_MONITOR_STARTED_AT,
        "last_cycle_at": _KEY_PROBE_MONITOR_LAST_CYCLE,
    }

PROVIDER_KEY_NAMES = {
    "xai": "果肉视频",
    "sora": "OpenAI Sora",
    "seedance": "火山 Seedance",
    "omni": "Gemini Omni",
    "minimax": "MiniMax H3",
}


def _probe_is_credential_rejection(probe):
    # 403 也可能只是模型/功能未开通；探针拿不到足够错误细节时宁可保留 Key。
    return int((probe or {}).get("http_status") or 0) == 401


_PROVIDER_KEY_PING_ATTEMPTS = {}


def probe_provider_keys(now=None):
    """Refresh stale encrypted video keys with the existing non-generating probes."""
    if provider_keys is None:
        return []
    now = int(now or time.time())
    checked = []
    for item in provider_keys.list_public():
        if item.get("state") != "active" or item.get("managed") is False:
            continue
        key_id = str(item.get("id") or "")
        last = max(
            int(item.get("last_checked_at") or 0),
            int(_PROVIDER_KEY_PING_ATTEMPTS.get(key_id) or 0),
        )
        if not key_id or now - last < PROVIDER_KEY_PING_INTERVAL:
            continue
        _PROVIDER_KEY_PING_ATTEMPTS[key_id] = now
        try:
            candidate = provider_keys.candidates(
                item["provider"], preferred_id=key_id,
            )[0]
            probe = probe_provider_secret(item["provider"], candidate["secret"])
            if probe.get("ok") or _probe_is_credential_rejection(probe):
                provider_keys.set_health(
                    key_id,
                    bool(probe.get("ok")),
                    probe.get("latency_ms"),
                    probe.get("error") or (
                        "HTTP %s" % probe.get("http_status")
                        if probe.get("http_status") else ""
                    ),
                )
            checked.append({
                "id": key_id, "provider": item["provider"],
                "ok": bool(probe.get("ok")),
            })
        except Exception:
            checked.append({"id": key_id, "provider": item["provider"], "ok": False})
    return checked


def probe_provider_secret(provider, secret):
    """Validate a candidate key with a non-generating authenticated GET."""
    provider = str(provider or "").strip().lower()
    secret = str(secret or "").strip()
    if provider not in PROVIDER_KEY_NAMES:
        raise ValueError("不支持的视频渠道")
    if len(secret) < 8:
        raise ValueError("API 密钥格式无效")
    if provider == "xai":
        base = (_env_value(["XAI_API_BASE"]) or "https://api.x.ai/v1").rstrip("/")
        return _ping_upstream(
            "GET", base + "/models",
            headers={"Authorization": "Bearer " + secret},
            proxy_url=_xai_proxy_url() if "api.x.ai" in base else "",
        )
    if provider == "sora":
        base = (_env_value(["OPENAI_BASE"]) or "https://api.openai.com").rstrip("/")
        url = base + "/videos?limit=1" if base.endswith("/v1") else base + "/v1/videos?limit=1"
        return _ping_upstream(
            "GET", url, headers={"Authorization": "Bearer " + secret},
            proxied="api.openai.com" in base,
        )
    if provider == "seedance":
        base = (
            _env_value(["ARK_BASE"])
            or "https://ark.cn-beijing.volces.com/api/v3"
        ).rstrip("/")
        return _ping_upstream(
            "GET",
            base + "/contents/generations/tasks?page_num=1&page_size=1",
            headers={"Authorization": "Bearer " + secret},
            proxied=False,
        )
    if provider == "minimax":
        base = (
            _env_value(["MINIMAX_API_BASE"])
            or "https://api.minimaxi.com"
        ).rstrip("/")
        return _ping_upstream(
            "GET", base + "/v2/query/video_generation?page_num=1&page_size=1",
            headers={"Authorization": "Bearer " + secret}, proxied=False,
        )
    base = (
        _env_value(["GEMINI_OMNI_BASE", "GEMINI_BASE"])
        or "https://generativelanguage.googleapis.com"
    ).rstrip("/")
    return _ping_upstream(
        "GET",
        base + "/v1beta/models/gemini-omni-flash-preview",
        headers={"x-goog-api-key": secret},
        proxied="googleapis.com" in base,
    )


def provider_key_list():
    if provider_keys is None:
        return {"configured": False, "items": [], "detail": "密钥池模块不可用"}
    try:
        items = provider_keys.list_public()
        return {
            "configured": provider_keys.vault_ready(),
            "items": items,
        }
    except Exception as exc:
        return {"configured": False, "items": [], "detail": str(exc)[:180]}


def _admin_audit(actor, action, target, detail, conn=None):
    now = int(time.time())
    values = (
        str(actor or "admin")[:80],
        str(action)[:80],
        str(target)[:120],
        json.dumps(detail or {}, ensure_ascii=False),
        now,
    )
    if conn is not None:
        conn.execute(
            "INSERT INTO admin_audit(actor, action, target, detail, created_at) VALUES(?,?,?,?,?)",
            values,
        )
        return
    with closing(db()) as audit_conn:
        audit_conn.execute(
            "INSERT INTO admin_audit(actor, action, target, detail, created_at) VALUES(?,?,?,?,?)",
            values,
        )
        audit_conn.commit()


def add_provider_key(actor, body):
    if provider_keys is None:
        raise RuntimeError("密钥池模块不可用")
    provider = str(body.get("provider") or "").strip().lower()
    label = str(body.get("label") or "").strip()
    secret = str(body.get("secret") or "").strip()
    probe = probe_provider_secret(provider, secret)
    if not probe.get("ok"):
        status = probe.get("http_status")
        suffix = "（HTTP %s）" % status if status else ""
        raise ValueError("API 检测未通过，请更换有效密钥%s" % suffix)
    item = provider_keys.add_key(provider, label, secret, actor, health=probe)
    _admin_audit(
        actor,
        "provider_key.add",
        item["id"],
        {
            "provider": item["provider"],
            "label": item["label"],
            "last4": item["last4"],
            "latency_ms": probe.get("latency_ms"),
        },
    )
    return {"ok": True, "item": item, "probe": probe}


def test_provider_key(actor, body):
    if provider_keys is None:
        raise RuntimeError("密钥池模块不可用")
    key_id = str(body.get("id") or "").strip()
    provider = str(body.get("provider") or "").strip().lower()
    if not key_id:
        raise ValueError("缺少 API 密钥编号")
    if key_id != "env":
        item = provider_keys.public_key(key_id)
        provider = item["provider"]
    candidates = provider_keys.candidates(provider, preferred_id=key_id)
    if not candidates:
        raise ValueError("API 密钥不存在")
    probe = probe_provider_secret(provider, candidates[0]["secret"])
    if key_id != "env":
        if probe.get("ok") or _probe_is_credential_rejection(probe):
            provider_keys.set_health(
                key_id,
                bool(probe.get("ok")),
                probe.get("latency_ms"),
                probe.get("error") or ("HTTP %s" % probe.get("http_status") if probe.get("http_status") else ""),
            )
    _admin_audit(
        actor,
        "provider_key.test",
        key_id,
        {
            "provider": provider,
            "ok": bool(probe.get("ok")),
            "http_status": probe.get("http_status"),
            "latency_ms": probe.get("latency_ms"),
        },
    )
    return {"ok": bool(probe.get("ok")), "probe": probe}


def delete_provider_key(actor, body):
    if provider_keys is None:
        raise RuntimeError("密钥池模块不可用")
    key_id = str(body.get("id") or "").strip()
    item = provider_keys.public_key(key_id)
    provider_keys.retire_key(key_id)
    _admin_audit(
        actor,
        "provider_key.retire",
        key_id,
        {
            "provider": item["provider"],
            "label": item["label"],
            "last4": item["last4"],
        },
    )
    return {"ok": True}


def reveal_provider_key(actor, body):
    if provider_keys is None:
        raise RuntimeError("密钥池模块不可用")
    key_id = str(body.get("id") or "").strip()
    item = provider_keys.public_key(key_id)
    secret = provider_keys.reveal_key(key_id)
    _admin_audit(
        actor,
        "provider_key.reveal",
        key_id,
        {
            "provider": item["provider"],
            "label": item["label"],
            "last4": item["last4"],
        },
    )
    return {"ok": True, "id": key_id, "secret": secret, "expires_in": 5}


def reveal_server_key(actor, body):
    channel = str(body.get("channel") or body.get("key") or "").strip().lower()
    item = KEY_GROUP_MAP.get(channel)
    if not item:
        raise ValueError("API 渠道不存在")
    values = _key_group_values(item)
    if not values:
        raise ValueError("该 API 渠道尚未配置密钥")
    _admin_audit(
        actor,
        "server_key.reveal",
        channel,
        {
            "env": [value["env"] for value in values],
            "last4": [value["value"][-4:] for value in values],
        },
    )
    return {
        "ok": True,
        "key": channel,
        "secrets": [
            {"env": value["env"], "secret": value["value"]}
            for value in values
        ],
        "expires_in": 5,
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
            meta = LOG_META_RE.search(line)
            request_id = meta.group("request_id") if meta else ""
            hq_code = "" if not meta or meta.group("hq_code") in (None, "-") else meta.group("hq_code")
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
            if q and q not in path and q not in request_id and q not in hq_code:
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
                        "duration_sec": float(meta.group("duration")) if meta else None,
                        "request_id": request_id,
                        "hq_code": hq_code,
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


def _hermes_func(method, path, event):
    if event == "authentication":
        return "IP12 · 登录验证"
    if event == "authorization":
        return "IP12 · 权限验证"
    if event == "rate_limit":
        return "IP12 · 请求限流"
    if event == "concurrency_limit":
        return "IP12 · 并发限制"
    if event == "storage_quota":
        return "IP12 · 存储空间"
    if path == "/api/conversations":
        return "IP12 · 新建项目" if method == "POST" else "IP12 · 项目列表"
    if path.startswith("/api/conversations/"):
        return "IP12 · 删除项目" if method == "DELETE" else "IP12 · 打开项目"
    for prefix, name in (
        ("/api/foundation-report/generate", "IP12 · 生成初稿 PDF"),
        ("/api/foundation-report/confirm", "IP12 · 确认初稿"),
        ("/api/foundation-report/", "IP12 · 查看 PDF"),
        ("/api/chat-complete", "IP12 · 完整对话"),
        ("/api/chat", "IP12 · 教练对话"),
        ("/api/generate-report", "IP12 · 生成模块报告"),
        ("/api/generate-deliverable", "IP12 · 生成交付物"),
        ("/api/jump-module", "IP12 · 切换模块"),
        ("/api/", "IP12 · 其他功能"),
    ):
        if path.startswith(prefix):
            return name
    return "IP12"


def _collect_hermes_entries(limit):
    entries = []
    for log_path in (p for p in HERMES_AUDIT_LOGS if p.exists()):
        try:
            lines = _tail_lines(log_path)
        except Exception:
            continue
        for line in lines:
            try:
                row = json.loads(line)
                timestamp = int(row.get("time") or 0)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            status = row.get("status")
            try:
                status_code = int(status)
            except (TypeError, ValueError):
                status_code = None
            failed = status_code >= 400 if status_code is not None else str(status) not in {"ok", "success"}
            local = time.localtime(timestamp)
            key = (local.tm_year, local.tm_mon, local.tm_mday, local.tm_hour, local.tm_min, local.tm_sec)
            duration_ms = row.get("duration_ms")
            try:
                duration_sec = float(duration_ms) / 1000 if duration_ms is not None else None
            except (TypeError, ValueError):
                duration_sec = None
            method = str(row.get("method") or "")[:12]
            path = str(row.get("path") or "")[:500]
            event = str(row.get("event") or "")[:80]
            entries.append((key, {
                "source": "ip12",
                "time": "%02d-%02d %02d:%02d:%02d" % key[1:],
                "user": str(row.get("username") or "-")[:120],
                "func": _hermes_func(method, path, event),
                "cat": "fail" if failed else "ok",
                "status_text": str(status or "-"),
                "duration_sec": duration_sec,
                "cost": None,
                "path": path,
                "method": method,
                "ip": str(row.get("ip") or "")[:80],
                "ua": "",
                "request_id": str(row.get("request_id") or "")[:128],
                "hq_code": error_contract.code_for(status_code) if failed and status_code is not None else "",
            }))
    entries.sort(key=lambda x: x[0], reverse=True)
    return entries[:limit]


def request_logs(limit=200, status="", q="", include_noise=False):
    """聚合各 nginx access log 尾部的后端 /api/ 请求日志（最新在前）。"""
    limit = max(1, min(int(limit or 200), 500))
    entries, message = _collect_request_entries(limit, str(status or "").strip(), str(q or "").strip(), include_noise)
    out = {"items": [item for _, item in entries], "limit": limit}
    if message:
        out["message"] = message
    return out


def activity_logs(days=7, limit=200, category="", q="", source="", include_noise=False, offset=0):
    """任务记录(jobs 库) + HTTP 请求(nginx) 合并成一条时间线，最新在前。

    category: '' | ok | fail | running（统一语义：任务 done/error/排队中 ↔ HTTP <400/>=400）
    source:   '' | job | http | ip12
    """
    limit = max(1, min(int(limit or 200), 100))
    offset = max(0, int(offset or 0))
    q = str(q or "").strip()
    category = str(category or "").strip()
    source = str(source or "").strip()
    merged, message = [], None
    source_limit = 500

    if source in ("", "http") and category != "running":
        # 成功/失败下推到采集层，避免"失败行被截断挤掉"
        entries, message = _collect_request_entries(
            source_limit, status=category if category in ("ok", "fail") else "", include_noise=include_noise
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
                        "duration_sec": it["duration_sec"],
                        "cost": None,
                        "path": it["path"],
                        "method": it["method"],
                        "ip": it["ip"],
                        "ua": it["ua"],
                        "request_id": it["request_id"],
                        "hq_code": it.get("hq_code") or "",
                    },
                )
            )

    if source in ("", "ip12") and category != "running":
        merged.extend(_collect_hermes_entries(source_limit))

    if source in ("", "job"):
        for j in call_logs(days, source_limit)["items"]:
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
                        "func": j["func"] + ((" · " + j["operation"]) if j.get("operation") else ""),
                        "cat": cat,
                        "status_text": j["status"],
                        "duration_sec": j["duration_sec"],
                        "cost": j["cost"],
                        "path": "任务 #%s" % j["id"],
                        "method": "",
                        "ip": "",
                        "ua": "",
                        "request_id": "",
                        "hq_code": "",
                    },
                )
            )

    matching = []
    for key, it in sorted(merged, key=lambda x: x[0], reverse=True):
        if category and it["cat"] != category:
            continue
        if q and all(q not in (it.get(field) or "") for field in ("path", "user", "func", "request_id", "hq_code")):
            continue
        matching.append(it)
    total = len(matching)
    items = matching[offset:offset + limit]
    out = {
        "items": items,
        "limit": limit,
        "offset": offset,
        "total": total,
        "days": days,
        "error_catalog": error_contract.public_catalog(),
    }
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
    out["checked_at"] = int(time.time())
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


def load_function_registry(services=None):
    pages = function_registry.list_pages()
    content = next(
        (item for item in (services or []) if item.get("key") == "content"), {}
    )
    health = content.get("detail") or {}
    for page in pages:
        for feature in page.get("functions", []):
            visibility_key = feature.get("surface_visibility_key")
            acceptance_key = feature.get("acceptance_health_key") or visibility_key
            feature["runtime_visible"] = (
                health.get(visibility_key) is True if visibility_key else True
            )
            feature["acceptance_health"] = (
                health.get(acceptance_key) if acceptance_key else None
            )
            selections = {}
            for group, config in feature.get("alternative_selections", {}).items():
                selected = _env_value([config["env"]]) or config.get("default")
                selections[group] = str(selected or "").strip().lower()
            feature["selected_alternatives"] = selections
    return pages


def load_pricing():
    return pricing.list_prices()


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


def save_pricing(actor, body):
    key = str(body.get("key") or body.get("rule") or "").strip()
    reason = str(body.get("reason") or "").strip()[:200]
    if not reason:
        raise ValueError("请填写改价原因")
    old = pricing.get_rule(key)
    item = pricing.set_price(key, body.get("points"), actor)
    detail = {
        "old_points": old["points"],
        "new_points": item["points"],
        "reason": reason,
    }
    with closing(db()) as conn:
        conn.execute(
            "INSERT INTO admin_audit(actor, action, target, detail, created_at) VALUES(?,?,?,?,?)",
            (actor, "pricing.update", key, json.dumps(detail, ensure_ascii=False), int(time.time())),
        )
        conn.commit()
    return item


def _empty_stats(message=None):
    return {
        "days": 7,
        "total": 0,
        "by_kind": [],
        "by_operation": [],
        "unmapped": [],
        "evidence_errors": [],
        "trend": [],
        "high_failure": [],
        "message": message,
    }


_XIAOLE_FEATURE_BY_CHANNEL = {
    "grok": "grok_video",
    "micro": "seedance_video",
    "omni": "omni_video",
    "minimax": "minimax_h3_video",
}

def _operation_feature_key(kind, channel=""):
    kind = str(kind or "unknown")
    if kind == "xiaole_video":
        return _XIAOLE_FEATURE_BY_CHANNEL.get(str(channel or "").lower(), kind)
    return kind


def _count_status(bucket, status, count=1):
    bucket["total"] += count
    if status in {"done", "completed"}:
        bucket["done"] += count
    elif status in {"error", "failed", "refunded"}:
        bucket["error"] += count
    elif status in {"pending", "queued", "running", "processing"}:
        bucket["running"] += count
    else:
        bucket["other"] += count


def _finish_stats(items):
    for item in items:
        terminal = item["done"] + item["error"]
        item["success_rate"] = round(item["done"] / terminal, 4) if terminal else 0
        item["failure_rate"] = round(item["error"] / terminal, 4) if terminal else 0
    return items


def _video_asset_evidence(job_ids):
    if not job_ids:
        return {}, None
    if not ASSET_DB.exists():
        return {}, "视频资产证据库不存在"
    placeholders = ",".join("?" for _ in job_ids)
    try:
        with closing(sqlite3.connect(str(ASSET_DB), timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """SELECT job_id,phase,provider_video_id,video_file,video_url,status,error,updated_at
                   FROM video_assets WHERE job_id IN (%s)""" % placeholders,
                tuple(job_ids),
            ).fetchall()
        return {int(row["job_id"]): dict(row) for row in rows if row["job_id"]}, None
    except sqlite3.Error:
        return {}, "视频资产证据读取失败"


def _verify_local_artifact(evidence):
    result_file = str(evidence.get("result_file") or "").strip()
    if not result_file:
        if evidence.get("result_url"):
            evidence["artifact_check"] = "reference_only"
        return evidence
    try:
        root = CONTENT_OUT.resolve()
        candidate = pathlib.Path(result_file)
        path = (candidate if candidate.is_absolute() else root / candidate).resolve()
        if path == root or root not in path.parents or not path.is_file() or path.stat().st_size <= 0:
            evidence.update({"artifact_check": "missing", "delivery_verified": False})
        elif path.suffix.lower() in {".mp4", ".mov", ".webm"}:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=12,
            )
            ok = probe.returncode == 0 and bool(probe.stdout.strip())
            evidence.update({
                "artifact_check": "decodable" if ok else "decode_failed",
                "delivery_verified": ok,
            })
        else:
            evidence.update({"artifact_check": "file_exists", "delivery_verified": True})
    except (OSError, subprocess.SubprocessError):
        evidence.update({"artifact_check": "decode_failed", "delivery_verified": False})
    return evidence


def _job_evidence(row, asset=None):
    asset = asset or {}
    status = str(row["status"] or "unknown").lower()
    cost = int(row["cost"] or 0)
    refunded = int(row["refunded"] or 0)
    result_url = str(row["result_url"] or asset.get("video_url") or "").strip()
    result_file = str(row["result_file"] or asset.get("video_file") or "").strip()
    provider_task_id = str(
        asset.get("provider_video_id") or row["provider_result_id"] or ""
    ).strip()
    if status in {"error", "failed"}:
        if cost <= 0:
            billing_state = "not_charged"
        elif refunded == 1:
            billing_state = "refunded"
        elif refunded == 2:
            billing_state = "refund_pending"
        else:
            billing_state = "needs_review"
    elif status in {"done", "completed"}:
        # jobs.cost is the intended charge, not proof that the points ledger agrees.
        billing_state = "unverified"
    else:
        billing_state = "in_flight"
    return _verify_local_artifact({
        "job_id": int(row["id"]),
        "project_id": None,
        "status": status,
        "phase": asset.get("phase"),
        "created_at": int(row["created_at"] or 0),
        "updated_at": int(row["updated_at"] or 0),
        "business_accepted": True,
        "provider_task_id": provider_task_id or None,
        "provider_accepted": True if provider_task_id else None,
        "completed": status in {"done", "completed"},
        "result_url": result_url or None,
        "result_file": result_file or None,
        "output_reference_present": bool(result_url or result_file),
        "delivery_verified": False,
        "artifact_check": "not_recorded",
        "cost": cost,
        "refund_state": refunded,
        "billing_state": billing_state,
        "asset_status": asset.get("status"),
        "error": str(row["error"] or asset.get("error") or "")[:300],
    })


def _e2e_job_evidence(job_id):
    if not job_id or not JOB_DB.exists():
        return None
    try:
        with closing(sqlite3.connect(str(JOB_DB), timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """SELECT id,status,cost,COALESCE(refunded,0) AS refunded,
                          COALESCE(error,'') AS error,created_at,updated_at,
                          CASE WHEN json_valid(result) THEN COALESCE(json_extract(result,'$.video_url'),json_extract(result,'$.url'),'') ELSE '' END AS result_url,
                          CASE WHEN json_valid(result) THEN COALESCE(json_extract(result,'$.video_file'),json_extract(result,'$.file'),'') ELSE '' END AS result_file,
                          CASE WHEN json_valid(result) THEN COALESCE(json_extract(result,'$.provider_video_id'),json_extract(result,'$.video_id'),'') ELSE '' END AS provider_result_id
                   FROM jobs WHERE id=?""",
                (int(job_id),),
            ).fetchone()
        if not row:
            return None
        assets, _ = _video_asset_evidence([int(job_id)])
        return _job_evidence(row, assets.get(int(job_id)))
    except (OSError, sqlite3.Error, subprocess.SubprocessError, ValueError):
        return None


def _e2e_stage(key, name, state, detail):
    return {"key": key, "name": name, "state": state, "detail": detail}


def _public_e2e_run(row):
    item = dict(row)
    transaction_key = str(item.pop("transaction_key", "") or "").strip()
    evidence = _e2e_job_evidence(item.get("job_id"))
    if evidence:
        job_status = evidence["status"]
        item["status"] = {
            "pending": "queued", "queued": "queued", "running": "running",
            "done": "completed", "completed": "completed",
            "error": "failed", "failed": "failed",
        }.get(job_status, job_status)
        item["error"] = evidence.get("error") or item.get("error") or ""
    billing_ok = (
        item.get("points_before") is not None
        and item.get("points_after") is not None
        and int(item["points_before"]) - int(item["points_after"]) == int(item.get("cost") or 0)
    )
    ledger = None
    get_transaction = getattr(points_domain, "get_points_transaction", None)
    if transaction_key and callable(get_transaction):
        try:
            ledger = get_transaction(transaction_key)
        except Exception:
            ledger = None
    try:
        ledger_ok = bool(
            ledger
            and str(ledger.get("username") or "") == str(item.get("username") or "")
            and int(ledger.get("delta") or 0) == -int(item.get("cost") or 0)
            and int(ledger.get("after_points") or 0) == int(item.get("points_after") or 0)
        )
    except (TypeError, ValueError):
        ledger_ok = False
    failed = item["status"] in {"failed", "unknown"}
    provider_id = evidence and evidence.get("provider_task_id")
    completed = bool(evidence and evidence.get("completed"))
    delivered = bool(evidence and evidence.get("delivery_verified"))
    refunded = bool(evidence and evidence.get("billing_state") == "refunded")
    billing_passed = refunded or (completed and billing_ok and ledger_ok)
    item["stages"] = [
        _e2e_stage("accepted", "后台测试受理", "passed", "已建立独立测试批次 " + item["run_id"][:8]),
        _e2e_stage("account", "专用账号与点数", "passed" if item.get("username") else "waiting",
                   ("专用测试账号已就绪" if item.get("username") else "正在取得专用账号") + (" · 提交前 %s 点" % item["points_before"] if item.get("points_before") is not None else "")),
        _e2e_stage("job", "业务接口 / job_id", "passed" if item.get("job_id") else ("failed" if failed else "waiting"),
                   "job_id=%s" % item["job_id"] if item.get("job_id") else (item.get("error") or "等待业务接口受理")),
        _e2e_stage("route", "渠道与凭据", "passed" if provider_id else ("failed" if failed else "waiting"),
                   "已进入真实供应商线路" if provider_id else "等待供应商提交；不会用免费连通探针冒充通过"),
        _e2e_stage("provider", "供应商接单", "passed" if provider_id else ("failed" if failed else "waiting"),
                   ("provider_task_id=" + str(provider_id)) if provider_id else "尚无供应商任务编号"),
        _e2e_stage("generation", "供应商生成", "passed" if completed else ("failed" if failed else "waiting"),
                   "已到 completed" if completed else (item.get("error") or "等待生成终态")),
        _e2e_stage("delivery", "作品交付", "passed" if delivered else ("failed" if completed else "waiting"),
                   ({"decodable": "文件存在且可解码", "file_exists": "文件存在且非空",
                     "decode_failed": "文件返回但无法解码", "missing": "作品文件缺失",
                     "reference_only": "只有作品地址，尚未完成本地验收"}.get(
                         (evidence or {}).get("artifact_check"), "等待作品文件"))),
        _e2e_stage("billing", "账务闭环", "passed" if billing_passed else ("failed" if failed else "waiting"),
                   "失败任务已退款" if refunded else ("扣点流水一致" if billing_passed else ("点数变化一致，尚未找到扣点流水" if billing_ok else "等待终态扣点 / 退款证据"))),
    ]
    item["evidence"] = evidence
    return item


def list_e2e_runs(limit=30):
    with closing(db()) as connection:
        rows = connection.execute(
            "SELECT * FROM admin_e2e_runs ORDER BY created_at DESC LIMIT ?",
            (max(1, min(int(limit or 30), 100)),),
        ).fetchall()
    stored_status = {row["run_id"]: row["status"] for row in rows}
    runs = [_public_e2e_run(row) for row in rows]
    now = int(time.time())
    terminal = {"completed", "failed"}
    with closing(db()) as connection:
        for run in runs:
            if run["status"] in terminal and run["status"] != stored_status[run["run_id"]]:
                connection.execute(
                    "UPDATE admin_e2e_runs SET status=?,error=?,updated_at=? WHERE run_id=?",
                    (run["status"], run.get("error") or "", now, run["run_id"]),
                )
        connection.commit()
    return runs


def _insert_e2e_run(actor, operation_id, *, status="submitting", batch_id="",
                    username="", cost=0, error=""):
    run_id = uuid.uuid4().hex
    now = int(time.time())
    with closing(db()) as connection:
        connection.execute(
            """INSERT INTO admin_e2e_runs(
                   run_id,batch_id,operation_id,username,status,cost,error,
                   created_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (run_id, batch_id, operation_id, username, status, int(cost or 0),
             str(error or "")[:300], actor, now, now),
        )
        connection.commit()
    return run_id


def _submit_e2e_run(run_id, admin_token, retry_capacity=False):
    with closing(db()) as connection:
        row = connection.execute(
            "SELECT * FROM admin_e2e_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if not row:
            raise ValueError("生产链测试批次不存在")
        operation_id = row["operation_id"]
        quoted_cost = int(row["cost"] or 0)
        connection.execute(
            "UPDATE admin_e2e_runs SET status='submitting',error='',updated_at=? WHERE run_id=?",
            (int(time.time()), run_id),
        )
        connection.commit()
    try:
        session = auth_admin_request(
            "/api/auth/admin/e2e/session", admin_token, method="POST", payload={}
        )
        account = session["account"]
        prepared = _e2e_prepare_operation(session, operation_id)
        cost = int(prepared["cost"])
        if quoted_cost and cost != quoted_cost:
            raise ValueError("实时价格已从 %s 点变化为 %s 点，本批次已安全停止该项目" % (quoted_cost, cost))
        if int(account.get("points") or 0) < cost:
            raise ValueError("专用测试账号点数不足：需要 %s 点，当前 %s 点" % (cost, account.get("points") or 0))
        payload = prepared["payload"]
        payload["qa_run_id"] = run_id
        endpoint = prepared["endpoint"]
        idem = "e2e:" + run_id
        transaction_key = "job-charge:%s:%s:%s" % (account["username"], endpoint, idem)
        result = _content_e2e_request(endpoint, session["token"], payload, idem, cost)
        with closing(db()) as connection:
            connection.execute(
                """UPDATE admin_e2e_runs SET username=?,status='queued',job_id=?,cost=?,
                          points_before=?,points_after=?,transaction_key=?,updated_at=? WHERE run_id=?""",
                (account["username"], int(result["job_id"]), cost, int(account["points"]),
                 int(result.get("points_left") or 0), transaction_key, int(time.time()), run_id),
            )
            connection.commit()
    except urllib.error.URLError as exc:
        with closing(db()) as connection:
            connection.execute(
                "UPDATE admin_e2e_runs SET status='unknown',error=?,updated_at=? WHERE run_id=?",
                ("提交结果未知，禁止自动重试：" + str(exc.reason)[:140], int(time.time()), run_id),
            )
            connection.commit()
        raise RuntimeError("提交结果未知；请先按测试批次核对，禁止再次点击")
    except Exception as exc:
        if retry_capacity and getattr(exc, "status", 0) == 429:
            with closing(db()) as connection:
                connection.execute(
                    "UPDATE admin_e2e_runs SET status='planned',error=?,updated_at=? WHERE run_id=?",
                    ("等待测试账号任务位：" + str(exc)[:240], int(time.time()), run_id),
                )
                connection.commit()
            return None
        with closing(db()) as connection:
            connection.execute(
                "UPDATE admin_e2e_runs SET status='failed',error=?,updated_at=? WHERE run_id=?",
                (str(exc)[:300], int(time.time()), run_id),
            )
            connection.commit()
        raise
    return next(run for run in list_e2e_runs(100) if run["run_id"] == run_id)


def start_e2e_run(actor, admin_token, operation_id):
    runner = function_registry.e2e_runner(operation_id)
    if not runner:
        raise ValueError("功能注册表中不存在该模式")
    if not runner.get("supported"):
        raise ValueError(runner.get("blocked_reason") or "该模式尚未接入后台托管测试")
    if not points_domain:
        raise RuntimeError("点数模块不可用")
    with E2E_RUN_LOCK:
        if any(run["status"] in E2E_ACTIVE_STATUSES for run in list_e2e_runs(100)):
            raise ValueError("已有一条生产链测试或验收批次在运行，请等待终态后再提交")
        run_id = _insert_e2e_run(actor, operation_id)
        return _submit_e2e_run(run_id, admin_token)


def _e2e_run_passed(run):
    stages = run.get("stages") or []
    return bool(run.get("status") == "completed" and stages
                and all(stage.get("state") == "passed" for stage in stages))


def _e2e_run_fresh(run):
    return bool(_e2e_run_passed(run)
                and int(run.get("updated_at") or run.get("created_at") or 0) >= int(time.time()) - 86400)


def _e2e_page_modes(page_key):
    pages = load_function_registry(service_status())
    page = next((item for item in pages if item.get("key") == page_key), None)
    if not page or page.get("inventory_status") != "verified":
        raise ValueError("该客户功能页尚未完成盘点，不能批量验收")
    return [mode for feature in page.get("functions") or []
            if feature.get("runtime_visible") is not False
            for mode in feature.get("modes") or []]


def e2e_batch_preflight(admin_token, page_key, include_fresh=False):
    if not points_domain:
        raise RuntimeError("点数模块不可用")
    runs = list_e2e_runs(100)
    active = next((run for run in runs if run.get("status") in E2E_ACTIVE_STATUSES), None)
    if active:
        blocker = ("上次提交结果未知；请先核对任务与扣点，禁止重复提交"
                   if active.get("status") == "unknown" else "已有生产链验收批次在运行")
        return {"page_key": page_key, "ready": False, "blocker": blocker,
                "items": [], "target_count": 0, "fresh_count": 0, "unprepared_count": 0,
                "total_cost": 0, "points": 0, "include_fresh": bool(include_fresh)}
    latest = {}
    for run in runs:
        latest.setdefault(run.get("operation_id"), run)
    modes = _e2e_page_modes(page_key)
    fresh_count = sum(1 for mode in modes if _e2e_run_fresh(latest.get(mode.get("key"), {})))
    unprepared_count = sum(1 for mode in modes if not (mode.get("validation") or {}).get("supported"))
    targets = [mode for mode in modes
               if (mode.get("validation") or {}).get("supported")
               and (include_fresh or not _e2e_run_fresh(latest.get(mode.get("key"), {})))]
    session = auth_admin_request("/api/auth/admin/e2e/session", admin_token, method="POST", payload={})
    account = session["account"]
    items = []
    for mode in targets:
        try:
            prepared = _e2e_prepare_operation(session, mode["key"])
            items.append({"operation_id": mode["key"], "ready": True,
                          "cost": int(prepared["cost"]), "blocker": ""})
        except Exception as exc:
            items.append({"operation_id": mode["key"], "ready": False,
                          "cost": 0, "blocker": str(exc)[:220]})
    ready_items = [item for item in items if item["ready"]]
    total_cost = sum(item["cost"] for item in ready_items)
    points = int(account.get("points") or 0)
    blocker = ""
    if not account.get("membership_active"):
        blocker = "专用测试账号会员未生效"
    elif not ready_items:
        blocker = "当前没有需要重测且测试包已准备的模式"
    elif points < total_cost:
        blocker = "专用测试账号点数不足：一键验收需要 %s 点，当前 %s 点" % (total_cost, points)
    return {
        "page_key": page_key, "ready": not blocker, "blocker": blocker,
        "account": account.get("username") or "专用测试账号", "points": points,
        "items": items, "target_count": len(items), "ready_count": len(ready_items),
        "blocked_count": len(items) - len(ready_items), "fresh_count": fresh_count,
        "unprepared_count": unprepared_count, "total_cost": total_cost,
        "include_fresh": bool(include_fresh),
    }


def _e2e_user_active_counts(username):
    if not username or not JOB_DB.exists():
        return {"total": 0, "by_kind": {}}
    try:
        with closing(sqlite3.connect(str(JOB_DB), timeout=10)) as connection:
            rows = connection.execute(
                """SELECT kind,COUNT(*) FROM jobs
                   WHERE username=? AND status IN ('pending','running') GROUP BY kind""",
                (username,),
            ).fetchall()
        by_kind = {str(kind): int(count) for kind, count in rows}
        return {"total": sum(by_kind.values()), "by_kind": by_kind}
    except sqlite3.Error:
        return {"total": 0, "by_kind": {}}


def _e2e_batch_caps(account_token):
    try:
        health = _content_e2e_get("/api/gen/health", account_token)
    except Exception:
        health = {}
    return {
        "total": int(health.get("max_user_active_jobs") or 5),
        "xiaole_video": int(health.get("max_user_active_xiaole_video") or 2),
        "sora_video": int(health.get("max_user_active_sora_video") or 1),
        "tryon": int(health.get("max_user_active_tryon") or 1),
        "cinematic": int(health.get("max_user_active_cinematic") or 2),
    }


def _e2e_batch_can_submit(operation_id, counts, caps):
    runner = function_registry.e2e_runner(operation_id) or {}
    kind = _e2e_kind((runner.get("endpoint") or {}).get("path"))
    if counts.get("total", 0) >= caps["total"]:
        return False
    return kind not in caps or counts.get("by_kind", {}).get(kind, 0) < caps[kind]


def _stop_planned_batch_rows(batch_id, message):
    with closing(db()) as connection:
        connection.execute(
            """UPDATE admin_e2e_runs SET status='failed',error=?,updated_at=?
               WHERE batch_id=? AND status='planned'""",
            (message[:300], int(time.time()), batch_id),
        )
        connection.commit()


def _run_e2e_batch(batch_id, admin_token):
    try:
        session = auth_admin_request(
            "/api/auth/admin/e2e/session", admin_token, method="POST", payload={}
        )
        username = session["account"]["username"]
        caps = _e2e_batch_caps(session["token"])
        deadline = time.time() + E2E_BATCH_DEADLINE_SECONDS
        while True:
            runs = [run for run in list_e2e_runs(100) if run.get("batch_id") == batch_id]
            if any(run.get("status") == "unknown" for run in runs):
                _stop_planned_batch_rows(batch_id, "批次遇到提交结果未知，为防重复扣点已停止后续项目")
                return
            planned = [run for run in runs if run.get("status") == "planned"]
            active = [run for run in runs if run.get("status") in {"submitting", "queued", "running"}]
            if not planned:
                if not active:
                    return
                time.sleep(5)
                continue
            if time.time() >= deadline:
                _stop_planned_batch_rows(batch_id, "批次超过两小时，尚未提交的项目已安全停止，未扣点")
                return
            counts = _e2e_user_active_counts(username)
            selected = next((run for run in planned
                             if _e2e_batch_can_submit(run["operation_id"], counts, caps)), None)
            if not selected:
                time.sleep(5)
                continue
            try:
                result = _submit_e2e_run(
                    selected["run_id"], admin_token, retry_capacity=True
                )
            except Exception as exc:
                if getattr(exc, "status", 0) in {401, 403} or any(
                    marker in str(exc) for marker in ("点数不足", "实时价格已")
                ):
                    _stop_planned_batch_rows(batch_id, "批次安全停止：" + str(exc)[:240])
                    return
                continue
            if result is None:
                time.sleep(5)
    except Exception as exc:
        _stop_planned_batch_rows(batch_id, "批次调度停止：" + str(exc)[:240])


def list_e2e_batches(runs=None):
    groups = {}
    for run in runs or list_e2e_runs(100):
        batch_id = str(run.get("batch_id") or "")
        if batch_id:
            groups.setdefault(batch_id, []).append(run)
    batches = []
    for batch_id, items in groups.items():
        passed = sum(1 for item in items if _e2e_run_passed(item))
        failed = sum(1 for item in items if item.get("status") in {"failed", "unknown"}
                     or any(stage.get("state") == "failed" for stage in item.get("stages") or []))
        active = sum(1 for item in items
                     if item.get("status") in {"planned", "submitting", "queued", "running"})
        status = "running" if active else ("completed_with_failures" if failed else "completed")
        batches.append({
            "batch_id": batch_id, "page_key": _e2e_operation_page_key(items[0]["operation_id"]),
            "status": status, "total": len(items), "passed": passed, "failed": failed,
            "waiting": max(0, len(items) - passed - failed),
            "total_cost": sum(int(item.get("cost") or 0) for item in items),
            "created_at": min(int(item.get("created_at") or 0) for item in items),
            "updated_at": max(int(item.get("updated_at") or 0) for item in items),
            "items": [{
                "operation_id": item["operation_id"], "status": item.get("status"),
                "error": item.get("error") or "",
                "passed": sum(1 for stage in item.get("stages") or [] if stage.get("state") == "passed"),
                "stages": len(item.get("stages") or []),
            } for item in items],
        })
    return sorted(batches, key=lambda item: item["created_at"], reverse=True)[:10]


def _e2e_operation_page_key(operation_id):
    for page in function_registry.list_pages():
        for feature in page.get("functions") or []:
            if any(mode.get("key") == operation_id for mode in feature.get("modes") or []):
                return page.get("key") or ""
    return ""


def start_e2e_batch(actor, admin_token, page_key, include_fresh=False):
    with E2E_RUN_LOCK:
        preflight = e2e_batch_preflight(admin_token, page_key, include_fresh=include_fresh)
        if not preflight.get("ready"):
            raise ValueError(preflight.get("blocker") or "当前批次不能运行")
        batch_id = uuid.uuid4().hex
        for item in preflight["items"]:
            _insert_e2e_run(
                actor, item["operation_id"], batch_id=batch_id,
                status="planned" if item["ready"] else "failed",
                username=preflight["account"], cost=item["cost"], error=item["blocker"],
            )
        now = int(time.time())
        with closing(db()) as connection:
            connection.execute(
                "INSERT INTO admin_audit(actor,action,target,detail,created_at) VALUES(?,?,?,?,?)",
                (actor, "e2e.batch.start", batch_id,
                 json.dumps({"page_key": page_key, "target_count": preflight["target_count"],
                             "ready_count": preflight["ready_count"],
                             "total_cost": preflight["total_cost"],
                             "include_fresh": bool(include_fresh)}, ensure_ascii=False), now),
            )
            connection.commit()
        threading.Thread(
            target=_run_e2e_batch, args=(batch_id, admin_token), daemon=True,
            name="admin-e2e-batch-" + batch_id[:8],
        ).start()
    return next(batch for batch in list_e2e_batches() if batch["batch_id"] == batch_id)


def _compose_operation_stat(since):
    if not VIDEO_COMPOSE_DB.exists():
        return None, None
    try:
        with closing(sqlite3.connect(str(VIDEO_COMPOSE_DB), timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            columns = {row["name"] for row in connection.execute(
                "PRAGMA table_info(video_compose_projects)"
            )}
            if not columns:
                return None, None
            error_sql = "COALESCE(error,'')" if "error" in columns else "''"
            rows = connection.execute(
                """SELECT id,status,output_file,output_asset_id,created_at,updated_at,%s AS error
                   FROM video_compose_projects WHERE created_at>=?
                   ORDER BY created_at DESC""" % error_sql,
                (since,),
            ).fetchall()
    except sqlite3.Error:
        return None, "一键成片证据读取失败"
    if not rows:
        return None, None
    bucket = {
        "operation": "video.one_click.compose", "total": 0, "done": 0,
        "error": 0, "running": 0, "other": 0,
    }
    for row in rows:
        status = str(row["status"] or "unknown").lower()
        _count_status(
            bucket,
            status if status in {"completed", "failed", "refunded"} else "running",
        )
    latest = rows[0]
    bucket["latest"] = {
        "job_id": None,
        "project_id": latest["id"],
        "status": latest["status"],
        "created_at": int(latest["created_at"] or 0),
        "updated_at": int(latest["updated_at"] or 0),
        "business_accepted": True,
        "business_id_type": "project_id",
        "provider_task_id": None,
        "provider_accepted": None,
        "provider_task_state": "not_applicable",
        "completed": latest["status"] == "completed",
        "result_file": latest["output_file"] or None,
        "result_url": None,
        "output_reference_present": bool(latest["output_file"]),
        "delivery_verified": False,
        "artifact_check": "not_recorded",
        "cost": 0,
        "refund_state": 0,
        "billing_state": "not_applicable",
        "balance_state": "not_applicable",
        "asset_status": "done" if latest["output_asset_id"] else None,
        "error": str(latest["error"] or "")[:300],
        "evidence_note": "一键成片使用本地 project_id，不经过外部供应商任务",
    }
    return _finish_stats([bucket])[0], None


def job_stats(days=7):
    days = max(1, min(int(days or 7), 90))
    since = int(time.time()) - days * 86400
    rows = []
    evidence_errors = []
    if not JOB_DB.exists():
        evidence_errors.append("任务证据库不存在")
    else:
        try:
            with closing(sqlite3.connect(str(JOB_DB), timeout=10)) as connection:
                connection.row_factory = sqlite3.Row
                columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
                refunded_sql = "COALESCE(refunded,0)" if "refunded" in columns else "0"
                error_sql = "COALESCE(error,'')" if "error" in columns else "''"
                result_sql = "result" if "result" in columns else "NULL"
                # ponytail: scan only the selected time window; add a rollup table when this
                # becomes measurably slower than the existing admin refresh budget.
                rows = connection.execute(
                    """SELECT id,kind,status,cost,%s AS refunded,%s AS error,created_at,updated_at,
                              date(created_at, 'unixepoch') AS day,
                              CASE WHEN json_valid(payload) THEN LOWER(COALESCE(json_extract(payload,'$.channel'),'')) ELSE '' END AS channel,
                              CASE WHEN json_valid(payload) THEN LOWER(COALESCE(json_extract(payload,'$.mode'),'')) ELSE '' END AS mode,
                              CASE WHEN json_valid(payload) THEN LOWER(COALESCE(json_extract(payload,'$.cine_mode'),'')) ELSE '' END AS cine_mode,
                              CASE WHEN json_valid(payload) THEN CAST(COALESCE(json_extract(payload,'$.line'),'') AS TEXT) ELSE '' END AS line,
                              CASE WHEN json_valid(payload) AND json_type(payload,'$.reference_images')='array'
                                   THEN json_array_length(payload,'$.reference_images') ELSE 0 END AS reference_count,
                              CASE WHEN json_valid(payload) THEN COALESCE(json_extract(payload,'$.batch_id'),'') ELSE '' END AS batch_id,
                              CASE WHEN json_valid(payload) THEN LOWER(COALESCE(json_extract(payload,'$.operation'),'')) ELSE '' END AS operation,
                              CASE WHEN json_valid(payload) THEN COALESCE(json_extract(payload,'$.upscale'),0) ELSE 0 END AS upscale,
                              CASE WHEN json_valid(payload) AND
                                        (json_type(payload,'$._short_drama_video')='object' OR
                                         json_type(payload,'$.short_drama_binding')='object')
                                   THEN 'short-drama' ELSE '' END AS source_page,
                              CASE WHEN json_valid(%s) THEN COALESCE(json_extract(%s,'$.video_url'),json_extract(%s,'$.image_url'),json_extract(%s,'$.url'),'') ELSE '' END AS result_url,
                              CASE WHEN json_valid(%s) THEN COALESCE(json_extract(%s,'$.video_file'),json_extract(%s,'$.image_file'),json_extract(%s,'$.file'),'') ELSE '' END AS result_file,
                              CASE WHEN json_valid(%s) THEN COALESCE(json_extract(%s,'$.provider_video_id'),json_extract(%s,'$.video_id'),json_extract(%s,'$.provider_avatar_id'),'') ELSE '' END AS provider_result_id
                       FROM jobs WHERE created_at>=? ORDER BY created_at DESC""" % (
                           refunded_sql, error_sql,
                           result_sql, result_sql, result_sql, result_sql,
                           result_sql, result_sql, result_sql, result_sql,
                           result_sql, result_sql, result_sql, result_sql,
                       ),
                    (since,),
                ).fetchall()
        except sqlite3.Error:
            evidence_errors.append("任务证据读取失败")
    by_kind = {}
    by_operation = {}
    unmapped = {}
    latest_rows = {}
    trend_counts = {}
    for row in rows:
        status = str(row["status"] or "unknown").lower()
        kind = _operation_feature_key(row["kind"], row["channel"])
        bucket = by_kind.setdefault(kind, {
            "kind": kind, "total": 0, "done": 0, "error": 0,
            "running": 0, "other": 0, "sources": [],
        })
        source = {"kind": str(row["kind"] or "unknown")}
        if row["channel"]:
            source["channel"] = str(row["channel"])
        if source not in bucket["sources"]:
            bucket["sources"].append(source)
        _count_status(bucket, status)
        metadata = {
            "channel": row["channel"], "mode": row["mode"],
            "cine_mode": row["cine_mode"], "line": row["line"],
            "reference_count": row["reference_count"], "batch_id": row["batch_id"],
            "operation": row["operation"], "upscale": row["upscale"],
            "source_page": row["source_page"],
        }
        operation = function_registry.classify_task(row["kind"], metadata)
        if operation:
            operation_bucket = by_operation.setdefault(operation, {
                "operation": operation, "total": 0, "done": 0, "error": 0,
                "running": 0, "other": 0,
            })
            _count_status(operation_bucket, status)
            latest_rows.setdefault(operation, row)
        else:
            page_key = str(row["source_page"] or "")
            if not page_key and str(row["kind"] or "") in {
                "video", "avatar", "cinematic", "tryon", "xiaole_video", "sora_video",
            }:
                page_key = "video"
            signature = str(row["kind"] or "unknown")
            for key in ("channel", "mode", "cine_mode", "line"):
                if row[key]:
                    signature += "/" + str(row[key])
            missing = unmapped.setdefault((page_key, signature), {
                "signature": signature, "kind": str(row["kind"] or "unknown"),
                "channel": str(row["channel"] or ""), "page_key": page_key, "total": 0,
                "done": 0, "error": 0, "running": 0, "other": 0,
                "latest_at": int(row["created_at"] or 0), "latest_error": "",
            })
            _count_status(missing, status)
            if not missing["latest_error"] and row["error"]:
                missing["latest_error"] = str(row["error"])[:220]
        trend_key = (row["day"], kind, status)
        trend_counts[trend_key] = trend_counts.get(trend_key, 0) + 1

    assets, asset_error = _video_asset_evidence(
        [int(row["id"]) for row in latest_rows.values()]
    )
    if asset_error:
        evidence_errors.append(asset_error)
    for operation, row in latest_rows.items():
        by_operation[operation]["latest"] = _job_evidence(
            row, assets.get(int(row["id"]))
        )
    compose, compose_error = _compose_operation_stat(since)
    if compose_error:
        evidence_errors.append(compose_error)
    if compose:
        by_operation[compose["operation"]] = compose
    items = _finish_stats(list(by_kind.values()))
    operation_items = _finish_stats(list(by_operation.values()))
    unmapped_items = _finish_stats(list(unmapped.values()))
    high_failure = [
        item for item in items
        if item["total"] >= 3 and item["failure_rate"] >= 0.5
    ]
    trend = [
        {"day": day, "kind": kind, "status": status, "count": count}
        for (day, kind, status), count in sorted(trend_counts.items())
    ]
    return {
        "days": days,
        "total": len(rows),
        "by_kind": sorted(items, key=lambda x: x["total"], reverse=True),
        "by_operation": sorted(operation_items, key=lambda x: x["operation"]),
        "unmapped": sorted(unmapped_items, key=lambda x: x["total"], reverse=True),
        "evidence_errors": evidence_errors,
        "trend": trend,
        "high_failure": sorted(high_failure, key=lambda x: x["failure_rate"], reverse=True),
        "message": "；".join(evidence_errors) if evidence_errors else None,
    }


_PAYLOAD_FIELD_RE = re.compile(r'"(model|provider|channel|mode|keyword|url|line)"\s*:\s*"([^"]*)"')


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
                      substr(payload, 1, 4096) AS payload, created_at, updated_at,
                      CASE WHEN json_valid(payload) THEN LOWER(COALESCE(json_extract(payload,'$.channel'),'')) ELSE '' END AS channel,
                      CASE WHEN json_valid(payload) THEN LOWER(COALESCE(json_extract(payload,'$.mode'),'')) ELSE '' END AS mode,
                      CASE WHEN json_valid(payload) THEN LOWER(COALESCE(json_extract(payload,'$.cine_mode'),'')) ELSE '' END AS cine_mode,
                      CASE WHEN json_valid(payload) THEN CAST(COALESCE(json_extract(payload,'$.line'),'') AS TEXT) ELSE '' END AS line,
                      CASE WHEN json_valid(payload) AND json_type(payload,'$.reference_images')='array'
                           THEN json_array_length(payload,'$.reference_images') ELSE 0 END AS reference_count,
                      CASE WHEN json_valid(payload) THEN COALESCE(json_extract(payload,'$.batch_id'),'') ELSE '' END AS batch_id,
                      CASE WHEN json_valid(payload) THEN LOWER(COALESCE(json_extract(payload,'$.operation'),'')) ELSE '' END AS operation,
                      CASE WHEN json_valid(payload) AND
                                (json_type(payload,'$._short_drama_video')='object' OR
                                 json_type(payload,'$.short_drama_binding')='object')
                           THEN 'short-drama' ELSE '' END AS source_page
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
        payload.update({
            "channel": row["channel"], "mode": row["mode"],
            "cine_mode": row["cine_mode"], "line": row["line"],
            "reference_count": row["reference_count"], "batch_id": row["batch_id"],
            "operation": row["operation"], "source_page": row["source_page"],
        })
        operation = function_registry.classify_task(kind, payload)
        duration = None
        if created_at and updated_at and updated_at >= created_at:
            duration = updated_at - created_at
        items.append(
            {
                "id": row["id"],
                "username": row["username"] or "-",
                "kind": kind,
                "func": call_func_name(kind, payload),
                "operation": operation,
                "cost": int(row["cost"] or 0),
                "status": row["status"] or "unknown",
                "created_at": created_at,
                "updated_at": updated_at,
                "duration_sec": duration,
            }
        )
    return {"days": days, "limit": limit, "items": items}


def user_job_insights(username):
    username = str(username or "").strip()
    if not username:
        raise ValueError("缺少用户账号")
    if len(username) > 64:
        raise ValueError("用户账号过长")
    empty = {
        "total": 0, "done": 0, "error": 0, "running": 0, "other": 0,
        "success_rate": 0, "by_function": [], "by_channel": [],
        "by_model": [], "recent": [],
    }
    if not JOB_DB.exists():
        return empty
    with closing(sqlite3.connect(str(JOB_DB), timeout=10)) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            """SELECT id,kind,cost,status,substr(payload,1,4096) AS payload,created_at
               FROM jobs WHERE username=? ORDER BY created_at DESC,id DESC""",
            (username,),
        ).fetchall()

    summary = dict(empty)
    groups = {"by_function": {}, "by_channel": {}, "by_model": {}}

    def add_group(group, name, status):
        item = group.setdefault(name or "未记录", {
            "name": name or "未记录", "total": 0, "done": 0, "error": 0,
        })
        item["total"] += 1
        if status in {"done", "error"}:
            item[status] += 1

    for row in rows:
        status = str(row["status"] or "unknown").lower()
        bucket = status if status in {"done", "error"} else (
            "running" if status in {"pending", "queued", "running"} else "other"
        )
        summary["total"] += 1
        summary[bucket] += 1
        payload = _job_payload(row["payload"])
        kind = row["kind"] or "unknown"
        channel = payload.get("channel") or payload.get("provider")
        if not channel and payload.get("line"):
            channel = "线路 " + str(payload["line"])
        add_group(groups["by_function"], call_func_name(kind, payload), bucket)
        add_group(groups["by_channel"], str(channel or "未记录"), bucket)
        add_group(groups["by_model"], str(payload.get("model") or "未记录"), bucket)
        if len(summary["recent"]) < 20:
            created_at = int(row["created_at"] or 0)
            summary["recent"].append({
                "id": int(row["id"]),
                "func": call_func_name(kind, payload),
                "channel": str(channel or "未记录"),
                "model": str(payload.get("model") or "未记录"),
                "status": status,
                "cost": int(row["cost"] or 0),
                "created_at": created_at,
            })
    settled = summary["done"] + summary["error"]
    summary["success_rate"] = round(
        summary["done"] / settled, 4,
    ) if settled else 0
    for key, values in groups.items():
        items = list(values.values())
        for item in items:
            settled = item["done"] + item["error"]
            item["success_rate"] = round(item["done"] / settled, 4) if settled else 0
        summary[key] = sorted(items, key=lambda item: (-item["total"], item["name"]))[:30]
    return summary


class H(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        req_id = error_contract.request_id(self.headers)
        public_obj, hq_code = error_contract.normalize(code, obj, req_id)
        error_contract.audit(code, obj, req_id, hq_code)
        body = json.dumps(public_obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if hq_code:
            self.send_header("X-HQ-Error-Code", hq_code)
            self.send_header("X-HQ-Request-ID", req_id)
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
        if path == "/api/admin/public/inspirations":
            try:
                return self._send(200, inspiration_cases.list_public(ADMIN_DB))
            except Exception:
                return self._send(500, {"detail": "灵感案例加载失败"})
        user = self._admin()
        if not user:
            return
        if path == "/api/admin/health":
            return self._send(200, {"ok": True, "service": "huangque-admin"})
        if path == "/api/admin/services":
            return self._send(200, {"items": service_status()})
        if path == "/api/admin/keys":
            return self._send(200, {"items": key_status()})
        if path == "/api/admin/provider-keys":
            return self._send(200, provider_key_list())
        if path == "/api/admin/channels":
            return self._send(200, {"items": load_channels()})
        if path == "/api/admin/features":
            return self._send(200, {"items": load_features()})
        if path == "/api/admin/pricing":
            return self._send(200, {"items": load_pricing()})
        if path == "/api/admin/short-drama/lipsync/health":
            if short_drama_lipsync_observability is None:
                return self._send(503, {"detail": "lipsync observability unavailable"})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                window = int((q.get("window_seconds") or ["3600"])[0])
                result = short_drama_lipsync_observability.health(
                    lipsync_db, window_seconds=window
                )
                result["rollout"] = short_drama_lipsync_rollout.get_config(
                    lipsync_db
                )
                result["providers"] = (
                    short_drama_lipsync_rollout.provider_controls(lipsync_db)
                )
                return self._send(200, result)
            except Exception as exc:
                return self._send(500, {"detail": str(exc)[:180]})
        if path == "/api/admin/short-drama/lipsync/diagnostics":
            if short_drama_lipsync_diagnostics is None:
                return self._send(503, {"detail": "lipsync diagnostics unavailable"})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            filters = {
                key: (q.get(key) or [""])[0]
                for key in (
                    "project_id", "job_id", "attempt_id", "provider_job_id",
                    "version_id", "trace_id",
                )
            }
            try:
                return self._send(
                    200,
                    short_drama_lipsync_diagnostics.query(
                        lipsync_db, filters,
                        actor=user.get("username") or "admin",
                        limit=(q.get("limit") or ["100"])[0],
                    ),
                )
            except ValueError as exc:
                return self._send(400, {"detail": str(exc)})
            except Exception as exc:
                return self._send(500, {"detail": str(exc)[:180]})
        if path == "/api/admin/announcements":
            q = urllib.parse.urlparse(self.path).query
            suffix = "/api/auth/admin/announcements" + (("?" + q) if q else "")
            try:
                return self._send(200, auth_admin_request(suffix, self._token()))
            except Exception as e:
                return auth_error_response(self, e)
        if path == "/api/admin/inspirations":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                return self._send(200, inspiration_cases.list_admin(
                    ADMIN_DB, JOB_DB, (q.get("days") or ["30"])[0]
                ))
            except Exception as exc:
                return self._send(500, {"detail": str(exc)[:180] or "案例加载失败"})
        if path == "/api/admin/users":
            q = urllib.parse.urlparse(self.path).query
            suffix = "/api/auth/admin/users" + (("?" + q) if q else "")
            try:
                return self._send(200, auth_admin_request(suffix, self._token()))
            except Exception as e:
                return auth_error_response(self, e)
        if path == "/api/admin/users/detail":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            username = (q.get("username") or [""])[0].strip()
            user_id = (q.get("user_id") or [""])[0].strip()
            if not username and not user_id:
                return self._send(400, {"detail": "缺少用户账号或 ID"})
            try:
                identity = ("user_id=" + urllib.parse.quote(user_id)) if user_id else (
                    "username=" + urllib.parse.quote(username)
                )
                data = auth_admin_request(
                    "/api/auth/admin/user-insights?" + identity,
                    self._token(),
                )
                data["tasks"] = user_job_insights(data["user"]["username"])
                return self._send(200, data)
            except ValueError as e:
                return self._send(400, {"detail": str(e)})
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
            "/api/admin/invite/reward-points", "/api/admin/invite/reward-claims",
            "/api/admin/invite/journeys", "/api/admin/invite/network",
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
            force = (q.get("force") or [""])[0].strip().lower() in {"1", "true", "yes"}
            if svc_key:
                svc = next((s for s in SERVICES if s["key"] == svc_key), None)
                if not svc:
                    return self._send(404, {"detail": "unknown service"})
                return self._send(200, probe_service(svc))
            if key_name:
                try:
                    return self._send(200, probe_key(key_name, force=force))
                except ValueError as exc:
                    return self._send(400, {"detail": str(exc)})
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
                        (q.get("offset") or ["0"])[0],
                    ),
                )
            except Exception as e:
                return self._send(500, {"detail": str(e)[:160]})
        if path == "/api/admin/stats":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(200, job_stats((q.get("days") or ["7"])[0]))
        if path == "/api/admin/e2e/preflight":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                return self._send(200, e2e_preflight(
                    self._token(), (q.get("operation_id") or [""])[0].strip()
                ))
            except ValueError as exc:
                return self._send(400, {"detail": str(exc)})
            except Exception as exc:
                return self._send(getattr(exc, "status", 500), {"detail": str(exc)[:220]})
        if path == "/api/admin/e2e/batch/preflight":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                return self._send(200, e2e_batch_preflight(
                    self._token(), (q.get("page_key") or [""])[0].strip(),
                    include_fresh=(q.get("include_fresh") or [""])[0].strip().lower()
                    in {"1", "true", "yes"},
                ))
            except ValueError as exc:
                return self._send(400, {"detail": str(exc)})
            except Exception as exc:
                return self._send(getattr(exc, "status", 500), {"detail": str(exc)[:220]})
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
            e2e_runs = list_e2e_runs()
            return self._send(
                200,
                {
                    "ok": True,
                    "user": {"username": user.get("username"), "name": user.get("name"), "role": user.get("role")},
                    "services": services,
                    "keys": key_status(),
                    "key_probes": key_probe_status(),
                    "key_probe_monitor": key_probe_monitor_status(),
                    "provider_keys": provider_key_list(),
                    "channels": load_channels(),
                    "function_registry": load_function_registry(services),
                    "features": load_features(services),
                    "pricing": load_pricing(),
                    "stats": job_stats(days),
                    "e2e_test": {
                        "configured": bool(E2E_TEST_USERNAME),
                        "username": E2E_TEST_USERNAME,
                    },
                    "e2e_runs": e2e_runs,
                    "e2e_batches": list_e2e_batches(e2e_runs),
                },
            )
        return self._send(404, {"detail": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/admin/"):
            return self._send(404, {"detail": "not found"})
        if path == "/api/admin/public/inspiration-events":
            try:
                if int(self.headers.get("Content-Length") or 0) > 16384:
                    raise ValueError("事件请求过大")
                return self._send(200, inspiration_cases.record_events(ADMIN_DB, self._body()))
            except ValueError as exc:
                return self._send(400, {"detail": str(exc)})
            except Exception:
                return self._send(500, {"detail": "事件记录失败"})
        user = self._admin()
        if not user:
            return
        if path == "/api/admin/e2e/run":
            try:
                body = self._body()
                if set(body) != {"operation_id", "confirmation"}:
                    raise ValueError("请求字段必须是 operation_id 和 confirmation")
                if body.get("confirmation") != "RUN":
                    raise ValueError("请明确确认本次真实扣点测试")
                run = start_e2e_run(
                    user.get("username") or "admin", self._token(),
                    str(body.get("operation_id") or "").strip(),
                )
                return self._send(200, {"ok": True, "run": run})
            except ValueError as exc:
                return self._send(400, {"detail": str(exc)})
            except Exception as exc:
                return self._send(getattr(exc, "status", 500), {"detail": str(exc)[:220]})
        if path == "/api/admin/e2e/batch/run":
            try:
                body = self._body()
                if not {"page_key", "confirmation"}.issubset(body) or not set(body).issubset(
                    {"page_key", "confirmation", "include_fresh"}
                ):
                    raise ValueError("请求字段必须是 page_key、confirmation 和可选 include_fresh")
                include_fresh = body.get("include_fresh") is True
                confirmation = "RERUN_BATCH" if include_fresh else "RUN_BATCH"
                if body.get("confirmation") != confirmation:
                    raise ValueError("请明确确认本次一键真实扣点验收")
                batch = start_e2e_batch(
                    user.get("username") or "admin", self._token(),
                    str(body.get("page_key") or "").strip(),
                    include_fresh=include_fresh,
                )
                return self._send(200, {"ok": True, "batch": batch})
            except ValueError as exc:
                return self._send(400, {"detail": str(exc)})
            except Exception as exc:
                return self._send(getattr(exc, "status", 500), {"detail": str(exc)[:220]})
        if path == "/api/admin/short-drama/lipsync/rollout":
            actor = user.get("username") or "admin"
            try:
                body = self._body()
                if str(body.get("confirmation") or "") != "CONFIRM":
                    raise ValueError("confirmation must be CONFIRM")
                result = short_drama_lipsync_rollout.set_config(
                    lipsync_db, actor, body,
                    expected_version=body.get("expected_version"),
                )
                feature_flags.set_enabled(
                    short_drama_lipsync_rollout.FEATURE,
                    bool(result["enabled"]), actor,
                )
                return self._send(200, {"ok": True, "rollout": result})
            except ValueError as exc:
                return self._send(400, {"detail": str(exc)})
            except short_drama_lipsync_rollout.RolloutError as exc:
                return self._send(exc.status, {
                    "detail": str(exc), "code": exc.code
                })
            except Exception as exc:
                return self._send(500, {"detail": str(exc)[:180]})
        if path == "/api/admin/short-drama/lipsync/provider":
            actor = user.get("username") or "admin"
            try:
                body = self._body()
                if str(body.get("confirmation") or "") != "CONFIRM":
                    raise ValueError("confirmation must be CONFIRM")
                result = short_drama_lipsync_rollout.set_provider_paused(
                    lipsync_db, actor, body.get("provider"),
                    bool(body.get("paused")), body.get("reason"),
                    incident_id=body.get("incident_id") or "",
                )
                return self._send(200, {"ok": True, "provider": result})
            except ValueError as exc:
                return self._send(400, {"detail": str(exc)})
            except Exception as exc:
                return self._send(500, {"detail": str(exc)[:180]})
        if path == "/api/admin/short-drama/lipsync/reconcile":
            if (
                short_drama_lipsync_reconcile is None
                or short_drama_lipsync_observability is None
            ):
                return self._send(503, {
                    "detail": "lipsync reconciliation unavailable"
                })
            actor = user.get("username") or "admin"
            try:
                body = self._body()
                if str(body.get("confirmation") or "") != "CONFIRM":
                    raise ValueError("confirmation must be CONFIRM")
                reason = str(body.get("reason") or "").strip()
                if not reason:
                    raise ValueError("reason is required")
                job_id = str(body.get("job_id") or "").strip()
                if not job_id:
                    raise ValueError("job_id is required")
                released = short_drama_lipsync_reconcile.release_expired_leases(
                    lipsync_db, now=int(time.time()), limit=1, job_id=job_id
                )
                changed = job_id in released
                short_drama_lipsync_observability.emit(
                    lipsync_db, "lipsync.admin.reconcile",
                    severity="warning", job_id=job_id, actor=actor,
                    detail={
                        "reason": reason,
                        "incident_id": body.get("incident_id") or "",
                        "changed": changed,
                    },
                )
                return self._send(200, {"ok": True, "changed": changed})
            except ValueError as exc:
                return self._send(400, {"detail": str(exc)})
            except Exception as exc:
                return self._send(500, {"detail": str(exc)[:180]})
        if path == "/api/admin/short-drama/lipsync/refund":
            if (
                short_drama_lipsync_rollout is None
                or short_drama_lipsync_jobs is None
                or short_drama_lipsync_observability is None
                or points_domain is None
            ):
                return self._send(503, {
                    "detail": "lipsync refund recovery unavailable"
                })
            actor = user.get("username") or "admin"
            try:
                body = self._body()
                if str(body.get("confirmation") or "") != "CONFIRM":
                    raise ValueError("confirmation must be CONFIRM")
                reason = str(body.get("reason") or "").strip()
                attempt_id = str(body.get("attempt_id") or "").strip()
                if not reason or not attempt_id:
                    raise ValueError("attempt_id and reason are required")
                claimed = short_drama_lipsync_rollout.request_manual_refund(
                    lipsync_db, actor, attempt_id, reason,
                    incident_id=body.get("incident_id") or "",
                )
                ledger = short_drama_lipsync_jobs.PointsLedger(points_domain)
                refunded = (
                    claimed["state"] == "refunded"
                    or short_drama_lipsync_jobs.reconcile_refund_attempt(
                        lipsync_db, ledger, attempt_id
                    )
                )
                short_drama_lipsync_observability.emit(
                    lipsync_db, "lipsync.admin.refund",
                    severity="warning", attempt_id=attempt_id, actor=actor,
                    detail={
                        "reason": reason,
                        "incident_id": body.get("incident_id") or "",
                        "old_state": claimed["attempt_state"],
                        "new_state": (
                            "refunded" if refunded else "refund_pending"
                        ),
                        "job_state": claimed["job_state"],
                        "refunded": refunded,
                    },
                )
                return self._send(200, {
                    "ok": True, "refunded": refunded,
                    "replayed": bool(claimed.get("replayed")),
                })
            except ValueError as exc:
                return self._send(400, {"detail": str(exc)})
            except short_drama_lipsync_rollout.RolloutError as exc:
                return self._send(exc.status, {
                    "detail": str(exc), "code": exc.code,
                })
            except Exception as exc:
                return self._send(500, {"detail": str(exc)[:180]})
        if path == "/api/admin/inspirations/media":
            try:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                result = inspiration_cases.upload_media(
                    self.rfile,
                    self.headers.get("Content-Length"),
                    self.headers.get("Content-Type"),
                    (q.get("kind") or [""])[0],
                )
                try:
                    _admin_audit(
                        user.get("username") or "admin", "inspiration.media.upload", result["key"],
                        {"media_type": result["media_type"], "size": result["size"]},
                    )
                except Exception as audit_error:
                    print("inspiration upload audit failed:", type(audit_error).__name__)
                return self._send(200, result)
            except ValueError as exc:
                return self._send(400, {"detail": str(exc)})
            except RuntimeError as exc:
                return self._send(503, {"detail": str(exc)})
            except Exception:
                return self._send(500, {"detail": "素材上传失败，请重试"})
        if path in {"/api/admin/inspirations/save", "/api/admin/inspirations/status"}:
            actor = user.get("username") or "admin"
            try:
                body = self._body()
                if path.endswith("/save"):
                    item = inspiration_cases.save_case(ADMIN_DB, body, actor, bool(body.get("publish")))
                    action = "publish" if body.get("publish") else "save"
                else:
                    status = str(body.get("status") or "")
                    item = inspiration_cases.set_status(ADMIN_DB, body.get("id"), status, actor)
                    action = status
                try:
                    _admin_audit(actor, "inspiration.%s" % action, item["id"], {
                        "title": item["title"], "status": item["status"], "public_id": item["public_id"],
                    })
                except Exception as audit_error:
                    print("inspiration audit failed:", type(audit_error).__name__)
                return self._send(200, {"ok": True, "item": item})
            except ValueError as exc:
                return self._send(400, {"detail": str(exc)})
            except Exception as exc:
                return self._send(500, {"detail": str(exc)[:180] or "保存失败"})
        if path == "/api/admin/server-keys/reveal":
            try:
                result = reveal_server_key(
                    user.get("username") or "admin", self._body()
                )
                return self._send(200, result)
            except ValueError as exc:
                return self._send(400, {"detail": str(exc)})
            except Exception as exc:
                return self._send(500, {"detail": str(exc)[:180] or "操作失败"})
        if path in {
            "/api/admin/provider-keys/add",
            "/api/admin/provider-keys/test",
            "/api/admin/provider-keys/delete",
            "/api/admin/provider-keys/reveal",
        }:
            actor = user.get("username") or "admin"
            try:
                body = self._body()
                if path.endswith("/add"):
                    result = add_provider_key(actor, body)
                elif path.endswith("/test"):
                    result = test_provider_key(actor, body)
                elif path.endswith("/reveal"):
                    result = reveal_provider_key(actor, body)
                else:
                    result = delete_provider_key(actor, body)
                return self._send(200, result)
            except ValueError as exc:
                return self._send(400, {"detail": str(exc)})
            except Exception as exc:
                if provider_keys is not None and isinstance(
                    exc, provider_keys.KeyStoreUnavailable
                ):
                    return self._send(503, {"detail": str(exc)})
                return self._send(500, {"detail": str(exc)[:180] or "操作失败"})
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
        if path == "/api/admin/pricing":
            try:
                item = save_pricing(user.get("username") or "admin", self._body())
            except (ValueError, KeyError) as e:
                return self._send(400, {"detail": str(e)})
            except Exception as e:
                return self._send(500, {"detail": str(e)[:160] or "保存失败"})
            return self._send(200, {"ok": True, "pricing": item})
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
        if path == "/api/admin/users/password/reset":
            try:
                body = self._body()
                if not isinstance(body, dict):
                    return self._send(400, {"detail": "请求体不是合法 JSON"})
                result = auth_admin_request(
                    "/api/auth/admin/password/reset", self._token(), method="POST", payload=body,
                )
                _admin_audit(
                    user.get("username") or "admin", "user_password_reset",
                    str(body.get("username") or ""), {"sessions_revoked": True, "must_change": True},
                )
                return self._send(200, result)
            except ValueError as e:
                return self._send(400, {"detail": str(e)})
            except Exception as e:
                return auth_error_response(self, e)
        if path == "/api/admin/announcements/preview":
            try:
                return self._send(200, auth_admin_request(
                    "/api/auth/admin/announcements/preview", self._token(),
                    method="POST", payload=self._body(),
                ))
            except ValueError as e:
                return self._send(400, {"detail": str(e)})
            except Exception as e:
                return auth_error_response(self, e)
        if path == "/api/admin/announcements":
            try:
                body = self._body()
                result = auth_admin_request(
                    "/api/auth/admin/announcements", self._token(), method="POST", payload=body,
                )
                campaign = result.get("campaign") or {}
                if not result.get("duplicate"):
                    try:
                        _admin_audit(
                            user.get("username") or "admin", "announcement_publish", campaign.get("id"),
                            {
                                "request_id": campaign.get("request_id"),
                                "audience": campaign.get("audience"),
                                "recipient_count": campaign.get("recipient_count", 0),
                                "wechat_push_requested": campaign.get("wechat_push_requested", False),
                                "wechat_recipient_count": campaign.get("wechat_recipient_count", 0),
                            },
                        )
                    except Exception as audit_error:
                        print("announcement publish audit failed:", type(audit_error).__name__)
                return self._send(200, result)
            except ValueError as e:
                return self._send(400, {"detail": str(e)})
            except Exception as e:
                return auth_error_response(self, e)
        if path.startswith("/api/admin/announcements/") and path.endswith("/recall"):
            suffix = path.replace("/api/admin/", "/api/auth/admin/", 1)
            try:
                result = auth_admin_request(
                    suffix, self._token(), method="POST", payload={},
                )
                campaign = result.get("campaign") or {}
                if not result.get("already_recalled"):
                    try:
                        _admin_audit(
                            user.get("username") or "admin", "announcement_recall", campaign.get("id"),
                            {"recipient_count": campaign.get("recipient_count", 0)},
                        )
                    except Exception as audit_error:
                        print("announcement recall audit failed:", type(audit_error).__name__)
                return self._send(200, result)
            except Exception as e:
                return auth_error_response(self, e)
        if path == "/api/admin/users/notification":
            try:
                body = self._body()
                result = auth_admin_request(
                    "/api/auth/admin/notifications", self._token(), method="POST", payload=body,
                )
                try:
                    _admin_audit(
                        user.get("username") or "admin", "user_notification",
                        str(body.get("username") or ""), {
                            "title": str(body.get("title") or "")[:80],
                            "detail_chars": len(str(body.get("detail") or "")),
                        },
                    )
                except Exception as audit_error:
                    print("admin notification audit failed:", type(audit_error).__name__)
                return self._send(200, result)
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
        if path == "/api/admin/membership/recharge":
            try:
                return self._send(
                    200,
                    auth_admin_request(
                        "/api/auth/admin/membership/recharge", self._token(), method="POST", payload=self._body(),
                    ),
                )
            except ValueError as e:
                return self._send(400, {"detail": str(e)})
            except Exception as e:
                return auth_error_response(self, e)
        if path == "/api/admin/membership/recharge/preview":
            try:
                return self._send(
                    200,
                    auth_admin_request(
                        "/api/auth/admin/membership/recharge/preview", self._token(), method="POST", payload=self._body(),
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
        if path.startswith("/api/admin/invite/reward-points/"):
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
    start_key_probe_monitor()
    print("huangque-admin on 127.0.0.1:%d" % PORT)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
