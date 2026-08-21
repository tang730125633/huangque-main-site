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
import http.client
from importlib import import_module
import base64
import binascii
import hashlib
import json
import mimetypes
import os
import pathlib
import re
import subprocess
import tempfile
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

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None

_DOMAIN_PACKAGE = (
    __package__ + ".content_domains" if __package__ else "content_domains"
)
egress = import_module(_DOMAIN_PACKAGE + ".egress")
feature_flags = import_module(_DOMAIN_PACKAGE + ".feature_flags")
function_registry = import_module(_DOMAIN_PACKAGE + ".function_registry")
provider_keys = import_module(_DOMAIN_PACKAGE + ".provider_keys")
pricing = import_module(_DOMAIN_PACKAGE + ".pricing")
error_contract = import_module(_DOMAIN_PACKAGE + ".error_contract")
video_minimax_h3 = import_module(_DOMAIN_PACKAGE + ".video_minimax_h3")


def _optional_content_domain(name):
    try:
        return import_module(_DOMAIN_PACKAGE + "." + name)
    except ImportError:
        return None


points_domain = _optional_content_domain("points")
cosyvoice_domain = _optional_content_domain("cosyvoice")
pixelle_video = _optional_content_domain("pixelle_video")
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
browser_qa = _optional_content_domain("browser_qa")

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
IMGGEN_BASE = os.environ.get("IMGGEN_BASE", "http://127.0.0.1:8101").rstrip("/")
LEADGEN_BASE = os.environ.get("LEADGEN_BASE", "http://127.0.0.1:8100").rstrip("/")
DL_BASE = os.environ.get("DL_BASE", "http://127.0.0.1:8097").rstrip("/")
AUTH_INTERNAL_TOKEN = os.environ.get("HQ_INTERNAL_TOKEN", "")
JOB_DB = pathlib.Path(os.environ.get("CONTENT_JOB_DB", str(BASE / "content_jobs.db")))
ASSET_DB = pathlib.Path(os.environ.get("AUDIO_DB", str(BASE / "audio_assets.db")))
VIDEO_COMPOSE_DB = pathlib.Path(os.environ.get("VIDEO_COMPOSE_DB", str(BASE / "video_compose.db")))
ADMIN_DB = pathlib.Path(os.environ.get("ADMIN_DB", str(BASE / "admin_config.db")))
QA_FIXTURE_DIR = pathlib.Path(os.environ.get("HQ_QA_FIXTURE_DIR", str(BASE / "qa_fixtures")))
CONTENT_OUT = pathlib.Path(os.environ.get("CONTENT_OUT", str(BASE / "content_out")))
E2E_TEST_USERNAME = os.environ.get("HQ_E2E_TEST_USERNAME", "").strip()
E2E_RUN_LOCK = threading.Lock()
E2E_FIXTURE_LOCK = threading.Lock()
E2E_ACTIVE_STATUSES = {
    "planned", "submitting", "browser_running", "queued", "running", "unknown",
}
E2E_STAGE_KEYS = (
    "accepted", "account", "job", "route",
    "provider", "generation", "delivery", "billing",
)
E2E_BATCH_DEADLINE_SECONDS = 2 * 60 * 60


class E2ESubmitRejected(RuntimeError):
    """The business API returned a definite HTTP rejection."""


class E2ESubmitUncertain(RuntimeError):
    """The POST may have been accepted; a new idempotency key is unsafe."""

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
     "features": ["图片生成 → 纳米香蕉", "视频模块 → Omni 视频", "文案编导 → 链接提示词反推"],
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
    {"key": "minimax", "name": "MetaSo MiniMax API", "category": "视频生成",
     "features": ["视频模块 → 麦克视频"], "env_features": [],
     "pool_features": ["视频模块 → 麦克视频"],
     "pool_base_env": [], "pool_base_default": video_minimax_h3.new_task_api_base(),
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
    {"key": "zhipu", "name": "智谱视觉 API", "category": "内容分析",
     "features": ["文案编导 → 链接分镜拆解"],
     "env_base_env": ["REVERSE_ZHIPU_BASE"], "env_base_default": "https://open.bigmodel.cn/api/paas/v4",
     "env": ["REVERSE_ZHIPU_KEY"]},
    {"key": "tikhub", "name": "TikHub API", "category": "内容采集 / 获客",
     "features": ["内容采集 → 抖音 / 小红书 / 视频号", "获客分析 → 评论与线索", "文案编导 → 公开视频下载"], "env": ["TIKHUB_KEY", "TIKHUB_API_KEY"]},
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
                acceptance_id TEXT NOT NULL DEFAULT '',
                evidence_json TEXT NOT NULL DEFAULT '{}',
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
        c.execute(
            """CREATE TABLE IF NOT EXISTS admin_e2e_fixture_attempts(
                fixture_key TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL
            )"""
        )
        columns = {row[1] for row in c.execute("PRAGMA table_info(admin_e2e_runs)")}
        if "batch_id" not in columns:
            c.execute("ALTER TABLE admin_e2e_runs ADD COLUMN batch_id TEXT NOT NULL DEFAULT ''")
        if "acceptance_id" not in columns:
            c.execute("ALTER TABLE admin_e2e_runs ADD COLUMN acceptance_id TEXT NOT NULL DEFAULT ''")
        if "evidence_json" not in columns:
            c.execute("ALTER TABLE admin_e2e_runs ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '{}'")
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
        c.execute(
            """UPDATE admin_e2e_runs SET status='unknown',error=?,updated_at=?
               WHERE operation_id='short_drama.live_action.script_planning'
                 AND status='running' AND job_id IS NULL""",
            ("后台在短剧多步骤验收中重启；将在下次质检前按原幂等键恢复并清理",
             int(time.time())),
        )
        for row in c.execute(
            """SELECT run_id,status,job_id,evidence_json FROM admin_e2e_runs
               WHERE status IN ('browser_running','queued','running')"""
        ).fetchall():
            try:
                evidence = json.loads(row["evidence_json"] or "{}")
            except (json.JSONDecodeError, TypeError, ValueError):
                evidence = {}
            browser = dict(evidence.get("browser") or {})
            if browser.get("status") != "running":
                continue
            browser.update({
                "status": "failed", "error": "后台重启，客户页自动质检已中断",
                "completed_at": int(time.time()),
            })
            evidence["browser"] = browser
            c.execute(
                """UPDATE admin_e2e_runs SET status=?,evidence_json=?,error=?,updated_at=?
                   WHERE run_id=?""",
                (row["status"] if row["job_id"] else "unknown",
                 json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
                 ("客户任务已受理，继续核对生产链；浏览器验收需稍后重跑"
                  if row["job_id"] else "客户提交是否到达业务接口未知，禁止自动重试"),
                 int(time.time()), row["run_id"]),
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


def _e2e_fixture_value(value):
    if not isinstance(value, str) or not value.startswith("@env/"):
        return _fixture_data_url(value)
    name = value[5:]
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", name):
        raise ValueError("测试参数名称无效")
    resolved = os.environ.get(name, "").strip()
    if not resolved:
        raise ValueError("测试参数未配置：" + name)
    return resolved


def _e2e_payload(operation_id, runner, ready_avatar_ids=None, ready_audio_voice_key=""):
    prefill = runner.get("prefill") or {}
    resolve = _e2e_fixture_value
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
    elif operation_id.startswith("video.grok.") or operation_id == "canvas.video.grok":
        references = [resolve(item) for item in prefill.get("reference_images") or []]
        payload.update({
            "channel": "grok", "operation": "generate", "model": "grok-imagine-video",
            "prompt": prompt, "duration": int(prefill.get("duration") or 5),
            "resolution": str(prefill.get("resolution") or ("720p" if references else "480p")),
            "ratio": str(prefill.get("ratio") or "9:16"),
            "reference_images": references,
        })
    elif operation_id.startswith("video.minimax."):
        payload.update({
            "channel": "minimax", "operation": "generate", "prompt": prompt,
            "duration": 5, "resolution": "2k", "ratio": "9:16",
            "reference_images": [resolve(item) for item in prefill.get("reference_images") or []],
        })
    elif operation_id.startswith("video.omni."):
        payload.update({
            "channel": "omni", "operation": "generate", "prompt": prompt,
            "duration": 4, "resolution": "720p", "ratio": "9:16",
            "reference_images": [resolve(item) for item in prefill.get("reference_images") or []],
        })
    elif operation_id.startswith("video.seedance.") or operation_id == "canvas.video.micro":
        payload.update({
            "channel": "micro", "operation": "generate", "prompt": prompt,
            "duration": int(prefill.get("duration") or 4),
            "resolution": str(prefill.get("resolution") or "480p"),
            "ratio": str(prefill.get("ratio") or "9:16"),
            "generate_audio": bool(prefill.get("generate_audio", False)), "upscale": False,
            "reference_images": [resolve(item) for item in prefill.get("reference_images") or []],
        })
    elif operation_id.startswith("audio.tts."):
        if not ready_audio_voice_key:
            raise ValueError("专用测试账号尚未登记可用测试音色")
        payload.update({
            "text": str(prefill.get("text") or "").strip(),
            "voice": ready_audio_voice_key,
            "speed": float(prefill.get("speed", 1.0)),
            "pitch": int(prefill.get("pitch", 0)),
            "volume": int(prefill.get("volume", 0)),
            "source_page": "audio",
        })
    elif operation_id.startswith("text_video."):
        if not ready_audio_voice_key:
            raise ValueError("专用测试账号未读取到文案成片公共音色")
        payload.update({
            "pipeline": "pixelle",
            "mode": str(prefill.get("mode") or "generate"),
            "text": str(prefill.get("text") or "").strip(),
            "template": str(prefill.get("template") or "1080x1920/image_default.html"),
            "style": str(prefill.get("style") or "realistic_commercial"),
            "voice": ready_audio_voice_key,
            "provider": "pixelle", "source_page": "text-video",
        })
    elif operation_id.startswith(("image.", "canvas.image.")) or operation_id == "script.output.image":
        parts = operation_id.split(".")
        canvas_image = parts[0] == "canvas"
        script_image = operation_id == "script.output.image"
        provider_index = 2 if canvas_image else 1
        payload.update({
            "prompt": prompt, "ratio": str(prefill.get("ratio") or "1:1"),
            "quality": str(prefill.get("quality") or "std"),
            "count": int(prefill.get("count") or 1),
            "source_page": "script" if script_image else ("canvas" if canvas_image else "banana"),
        })
        provider = "openai" if script_image else parts[provider_index]
        payload["provider"] = provider
        if provider == "banana":
            payload["model"] = parts[provider_index + 1]
        elif provider == "seedream":
            payload["variant"] = parts[provider_index + 1]
        if operation_id == "image.openai.inpaint":
            payload["image"] = resolve(prefill["image_url"])
            payload["mask"] = resolve(prefill["mask_url"])
        else:
            references = [resolve(item) for item in prefill.get("reference_images") or []]
            if references:
                payload["reference_images"] = references
    elif operation_id.startswith("collect.content."):
        payload.update({
            "url": resolve(prefill["url"]), "want": list(prefill.get("want") or []),
            "provider": "tikhub", "source_page": "collect",
        })
    elif operation_id == "leads.keyword.search":
        payload.update({
            "keyword": str(prefill.get("keyword") or "").strip(),
            "platforms": list(prefill.get("platforms") or ["douyin"]),
            "count": int(prefill.get("count") or 1),
            "pages": int(prefill.get("pages") or 1),
            "channels_targets": list(prefill.get("channels_targets") or []),
            "provider": "tikhub", "source_page": "leads",
        })
    elif operation_id.startswith("script.write."):
        payload.update({
            "prompt": prompt,
            "format": str(prefill.get("format") or "script"),
            "style": str(prefill.get("style") or "口播"),
            "dur": str(prefill.get("dur") or "15s"),
            "platform": str(prefill.get("platform") or "抖音"),
            "ctype": str(prefill.get("ctype") or "分镜脚本"),
            "source_page": "script",
        })
    elif operation_id.startswith("script.breakdown.local_"):
        media_type = str(prefill.get("media_type") or "").strip().lower()
        payload.update({
            "file_data": resolve(prefill["file_url"]), "media_type": media_type,
            "mode": "reverse_prompt", "source_page": "script",
            "source_type": media_type,
        })
    elif operation_id.startswith("script.breakdown."):
        payload.update({
            "url": resolve(prefill["url"]),
            "mode": str(prefill.get("mode") or "scenes"),
            "source_page": "script",
        })
    elif operation_id in {
            "short_drama.live_action.script_planning",
            "short_drama.live_action.character_reference",
            "short_drama.live_action.shot_video",
            "short_drama.live_action.preview",
            "short_drama.live_action.delivery"}:
        payload = dict(prefill)
    elif operation_id == "canvas.agent.plan":
        payload.update({
            "prompt": prompt,
            "project_id": "qa-canvas-registry",
            "snapshot_digest": "a1b2c3d4e5f60718",
            "scope": "local",
            "nodes": [{
                "id": "qa_product", "type": "text", "title": "产品卖点",
                "content": "琥珀色保湿精华；清爽、易吸收；不得虚构品牌或功效",
                "selected": True,
            }],
            "edges": [], "selected_node_ids": ["qa_product"], "history": [],
            "quoted_cost": int(pricing.get_price("canvas.agent")),
            "page_context": {"page": "canvas", "path": "/workbench/canvas.html",
                             "title": "无限画布", "can_edit": True, "selected_count": 1},
            "source_page": "canvas",
        })
    else:
        raise ValueError("该模式尚未接入后台托管测试")
    if operation_id.startswith("canvas.video."):
        payload["source_page"] = "canvas"
    return payload


def _content_e2e_request(path, account_token, payload, idempotency_key, expected_cost,
                         require_job_id=True, method="POST"):
    headers = {
        "Authorization": "Bearer " + account_token,
        "Content-Type": "application/json; charset=utf-8",
        "Idempotency-Key": idempotency_key,
        "X-HQ-Internal-Token": AUTH_INTERNAL_TOKEN,
        "X-HQ-Expected-Cost": str(int(expected_cost)),
    }
    base = (IMGGEN_BASE if path == "/api/gen/banana" else
            LEADGEN_BASE if path in {"/api/gen/collect", "/api/gen/leads"} else CONTENT_BASE)
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=35) as response:
            raw = response.read()
        try:
            result = json.loads(raw or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError) as exc:
            raise E2ESubmitUncertain("业务接口响应不完整") from exc
        if not isinstance(result, dict):
            raise E2ESubmitUncertain("业务接口响应不是对象")
        if require_job_id and not result.get("job_id"):
            raise E2ESubmitUncertain("业务接口响应缺少 job_id")
        return result
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"{}")
        except Exception:
            body = {}
        definite_rejection = exc.code == 409 and body.get("code") == "quote_cost_changed"
        error_type = E2ESubmitUncertain if (
            not definite_rejection and (exc.code >= 500 or exc.code in {408, 409})
        ) else E2ESubmitRejected
        err = error_type(body.get("detail") or "业务接口拒绝测试任务")
        err.status = exc.code
        err.body = body
        raise err
    except E2ESubmitUncertain:
        raise
    except (urllib.error.URLError, TimeoutError, ConnectionError,
            http.client.IncompleteRead, http.client.RemoteDisconnected,
            OSError) as exc:
        raise E2ESubmitUncertain("提交连接中断或响应超时") from exc


def _content_e2e_upload(path, account_token, payload, idempotency_key, expected_cost):
    data_url = str(payload.get("file_data") or "")
    try:
        header, encoded = data_url.split(",", 1)
        content_type = header[5:].split(";", 1)[0].strip().lower()
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValueError("后台私有二进制测试素材无效") from exc
    allowed = {
        "image": {"image/jpeg", "image/png", "image/webp"},
        "video": {"video/mp4", "video/quicktime", "video/webm"},
    }
    media_type = str(payload.get("media_type") or "").strip().lower()
    if not raw or content_type not in allowed.get(media_type, set()):
        raise ValueError("后台私有二进制测试素材格式不匹配")
    headers = {
        "Authorization": "Bearer " + account_token,
        "Content-Type": content_type,
        "Content-Length": str(len(raw)),
        "Idempotency-Key": idempotency_key,
        "X-HQ-Internal-Token": AUTH_INTERNAL_TOKEN,
        "X-HQ-Expected-Cost": str(int(expected_cost)),
        "X-HQ-QA-Run-ID": str(payload.get("qa_run_id") or ""),
    }
    request = urllib.request.Request(CONTENT_BASE + path, data=raw, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            result = json.loads(response.read() or b"{}")
        if not isinstance(result, dict) or not result.get("job_id"):
            raise E2ESubmitUncertain("业务接口响应缺少 job_id")
        return result
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"{}")
        except Exception:
            body = {}
        error_type = E2ESubmitUncertain if exc.code >= 500 or exc.code in {408, 409} else E2ESubmitRejected
        err = error_type(body.get("detail") or "业务接口拒绝二进制测试任务")
        err.status = exc.code
        err.body = body
        raise err
    except E2ESubmitUncertain:
        raise
    except (urllib.error.URLError, TimeoutError, ConnectionError,
            http.client.IncompleteRead, http.client.RemoteDisconnected,
            OSError, json.JSONDecodeError) as exc:
        raise E2ESubmitUncertain("二进制提交连接中断或响应超时") from exc


def _content_e2e_get(path, account_token, timeout=20):
    req = urllib.request.Request(
        CONTENT_BASE + path,
        headers={"Authorization": "Bearer " + account_token},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
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
    endpoint = str(endpoint or "").split("?", 1)[0]
    return {
        "/api/gen/image": "image", "/api/gen/banana": "image",
        "/api/gen/audio": "audio",
        "/api/gen/video": "video", "/api/gen/tryon": "tryon",
        "/api/gen/xiaole_video": "xiaole_video", "/api/gen/sora_video": "sora_video",
        "/api/gen/cinematic": "cinematic",
        "/api/gen/collect": "collect", "/api/gen/leads": "leads",
        "/api/gen/copy": "copy", "/api/gen/breakdown": "breakdown",
        "/api/gen/breakdown/local-upload": "breakdown",
        "/api/gen/canvas_agent": "canvas_agent",
        "/api/gen/script_to_video": "script_to_video",
    }.get(endpoint)


def _ready_avatar_ids(operation_id, account_token):
    if not (operation_id.startswith("video.cinematic.")
            or operation_id in {
                "short_drama.live_action.shot_video",
                "short_drama.live_action.preview",
            }):
        return []
    avatar_data = _content_e2e_get("/api/gen/video/avatars?limit=120", account_token)
    return [
        item.get("id") for item in avatar_data.get("items") or []
        if item.get("status") == "ready" and item.get("id")
    ]


def _ready_audio_voice_key(operation_id, account_token):
    if operation_id.startswith("text_video."):
        voice_data = _content_e2e_get("/api/gen/text-video/voices", account_token)
        public = next((item for item in voice_data.get("voices") or []
                       if item.get("scope") == "public" and item.get("id")), None)
        if not public:
            raise ValueError("专用测试账号未读取到文案成片公共音色")
        return str(public["id"])
    if not operation_id.startswith("audio.tts."):
        return ""
    voice_data = _content_e2e_get("/api/gen/audio/voices", account_token)
    voices = voice_data.get("items") or []
    if operation_id == "audio.tts.public":
        public = next((item for item in voices
                       if item.get("scope") == "public" and item.get("voice_key")), None)
        if not public:
            raise ValueError("专用测试账号未读取到公共音色")
        return str(public["voice_key"])
    slot_data = _content_e2e_get("/api/gen/audio/slots", account_token)
    ready_ids = {
        int(item["voice_id"]) for item in slot_data.get("items") or []
        if item.get("status") == "ready" and item.get("voice_id")
    }
    clone_model = str(getattr(cosyvoice_domain, "CLONE_MODEL", "cosyvoice-") or "cosyvoice-")
    personal = next((item for item in voices
                     if item.get("scope") == "personal"
                     and item.get("id") in ready_ids
                     and str(item.get("provider_voice") or "").startswith(clone_model)
                     and item.get("voice_key")), None)
    if not personal:
        raise ValueError("专用测试账号个人测试音色尚未准备")
    return str(personal["voice_key"])


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
    if operation_id.startswith("audio.tts."):
        parameters.extend([
            "音色范围：%s" % ("个人" if operation_id.endswith(".personal") else "公共"),
            "文案长度：%s 字" % len(str(payload.get("text") or "")),
            "语速：%s" % payload.get("speed"),
            "音调：%s" % payload.get("pitch"),
            "音量：%s" % payload.get("volume"),
        ])
    elif operation_id.startswith("text_video."):
        parameters.extend([
            "输入方式：%s" % ("主题创作" if payload.get("mode") == "generate" else "完整文案"),
            "模板：%s" % payload.get("template"),
            "素材风格：%s" % payload.get("style"),
            "公共中文音色：已准备",
        ])
    elif operation_id.startswith(("image.", "canvas.image.")) or operation_id == "script.output.image":
        provider = payload.get("provider")
        provider_label = {
            "banana": "纳米香蕉", "openai": "黄雀引擎 2",
            "seedream": "黄雀引擎 1", "xiaole": "果肉生图",
        }.get(provider, provider)
        references = payload.get("reference_images") or ([] if not payload.get("image") else [payload["image"]])
        parameters.extend([
            "生成线路：%s" % provider_label,
            "清晰度：%s" % payload.get("quality"),
            "参考图：%s 张" % len(references),
        ])
        if payload.get("model"):
            parameters.append("型号：%s" % payload["model"])
        if payload.get("variant"):
            parameters.append("型号：%s" % payload["variant"])
        if payload.get("mask"):
            parameters.append("局部蒙版：已准备")
    elif operation_id.startswith("collect.content."):
        mode = next(iter(payload.get("want") or []), "comments")
        parameters.extend([
            "固定授权链接：已准备",
            "验收内容：%s" % {"comments": "内容与评论", "video": "视频解析下载",
                             "transcript": "口播文案"}.get(mode, mode),
        ])
    elif operation_id == "leads.keyword.search":
        parameters.extend([
            "平台：抖音", "关键词：预设通用关键词",
            "最低采集量：%s" % payload.get("count", 1),
        ])
    elif operation_id.startswith("script.write."):
        parameters.extend([
            "脚本风格：%s" % payload.get("style"),
            "目标时长：%s" % payload.get("dur"),
            "发布平台：%s" % payload.get("platform"),
        ])
    elif operation_id.startswith("script.breakdown.local_"):
        parameters.extend([
            "私有测试素材：已准备",
            "素材类型：%s" % ("本地图片" if payload.get("media_type") == "image" else "本地短视频"),
            "验收内容：非空视觉提示词",
        ])
    elif operation_id.startswith("script.breakdown."):
        parameters.extend([
            "固定授权链接：已准备",
            "验收内容：%s" % ("视频提示词" if payload.get("mode") == "reverse_prompt" else "分镜结构"),
        ])
    elif operation_id == "short_drama.live_action.script_planning":
        parameters.extend([
            "固定真人短剧：2 个角色、30 秒、6 个分镜",
            "验收范围：导入、确认、生成、锁定、制作预检与自动清理",
            "费用：0 点，不执行角色图或视频生成",
        ])
    elif operation_id == "short_drama.live_action.character_reference":
        parameters.extend([
            "固定真人短剧：1 个文字角色卡",
            "验收范围：导入、保存角色、生成并锁定标准图、自动清理",
            "生成线路：纳米香蕉 2 · 高清 · 3:4 · 1 张",
        ])
    elif operation_id == "short_drama.live_action.shot_video":
        parameters.extend([
            "固定真人短剧：1 个 5 秒镜头",
            "固定素材：专用测试账号已就绪形象",
            "验收范围：预检、报价、Grok 接单、视频解码、扣点与自动清理",
        ])
    elif operation_id == "short_drama.live_action.preview":
        parameters.extend([
            "固定真人短剧：30 秒、6 个真实镜头",
            "素材策略：首次生成后保留为私有测试快照",
            "验收范围：Grok 逐镜作品、720p 合成、文件解码与扣点闭环",
        ])
    elif operation_id == "short_drama.live_action.delivery":
        parameters.extend([
            "固定输入：已验收的 30 秒私有预览",
            "验收范围：六项确认、0 点报价、1080p 导出、音视频与账务闭环",
            "素材策略：复用私有快照，不重复生成付费镜头",
        ])
    elif operation_id == "canvas.agent.plan":
        parameters.extend([
            "固定画布：1 个产品卖点文本节点",
            "执行边界：只返回计划，不自动应用或生成媒体",
        ])
    return parameters


def _e2e_prepare_operation(session, operation_id):
    runner = function_registry.e2e_runner(operation_id)
    if not runner:
        raise ValueError("功能注册表中不存在该模式")
    if not runner.get("supported"):
        raise ValueError(runner.get("blocked_reason") or "该模式尚未接入后台托管测试")
    disabled_flags = [key for key in runner.get("flag_keys") or []
                      if not feature_flags.is_enabled(key)]
    if disabled_flags:
        raise ValueError("该模式已暂停接单，自动质检不会提交付费任务")
    account_token = session["token"]
    account = session["account"]
    avatars = _ready_avatar_ids(operation_id, account_token)
    audio_voice_key = _ready_audio_voice_key(operation_id, account_token)
    payload = _e2e_payload(operation_id, runner, avatars, audio_voice_key)
    endpoint = runner["endpoint"]["path"]
    if operation_id == "short_drama.live_action.script_planning":
        return {
            "operation_id": operation_id, "runner": runner, "payload": payload,
            "endpoint": endpoint, "kind": "short_drama_project", "cost": 0,
            "parameters": _e2e_parameters(operation_id, payload),
        }
    if operation_id == "short_drama.live_action.character_reference":
        image_payload = {
            "provider": "banana", "model": "nb2", "quality": "hd",
            "ratio": "3:4", "count": 1, "prompt": "后台短剧角色标准图质检",
        }
        return {
            "operation_id": operation_id, "runner": runner, "payload": payload,
            "endpoint": endpoint, "kind": "image",
            "cost": int(points_domain.cost_of("image", image_payload)),
            "parameters": _e2e_parameters(operation_id, payload),
        }
    if operation_id == "short_drama.live_action.shot_video":
        selected = str(
            os.environ.get("HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER") or ""
        ).strip().lower()
        if selected not in {"grok", "grok_xai"}:
            raise ValueError("短剧逐镜尚未选择 Grok 真实生产线路")
        if provider_keys is None or not provider_keys.has_candidate("xai"):
            raise ValueError("短剧逐镜缺少可用的 xAI / Grok 凭据")
        if not avatars:
            raise ValueError("专用测试账号尚未登记可用的固定测试形象")
        model = str(
            os.environ.get("HQ_SHORT_DRAMA_GROK_MODEL")
            or "grok-imagine-video"
        )
        cost = int(points_domain.cost_of("xiaole_video", {
            "channel": "grok", "model": model,
            "resolution": "720p", "duration": 5,
        }))
        return {
            "operation_id": operation_id, "runner": runner, "payload": payload,
            "endpoint": endpoint, "kind": "short_drama_provider_shot",
            "cost": cost, "avatar_id": int(avatars[0]),
            "parameters": _e2e_parameters(operation_id, payload),
        }
    if operation_id == "short_drama.live_action.preview":
        selected = str(
            os.environ.get("HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER") or ""
        ).strip().lower()
        if selected not in {"grok", "grok_xai"}:
            raise ValueError("短剧预览尚未选择 Grok 真实生产线路")
        if provider_keys is None or not provider_keys.has_candidate("xai"):
            raise ValueError("短剧预览缺少可用的 xAI / Grok 凭据")
        if not avatars:
            raise ValueError("专用测试账号尚未登记可用的固定测试形象")
        fixture = _short_drama_preview_fixture_state(account["username"])
        if fixture and any(
            str(item.get("status") or "") in {
                "billing", "queued", "submitting", "running", "submit_unknown",
            }
            for item in (fixture.get("latest_jobs") or {}).values()
            if str(item.get("shot_key") or "")
            in set(fixture.get("missing_shot_keys") or [])
        ):
            raise ValueError("短剧预览私有逐镜素材仍在生成，请等待终态")
        missing_keys = list(fixture.get("missing_shot_keys") or []) if fixture else []
        durations = (
            [
                max(1, (int((fixture.get("shot_durations") or {}).get(key) or 0) + 999) // 1000)
                for key in missing_keys
            ]
            if fixture else [15, max(1, int(payload.get("target_duration") or 30) - 15)]
        )
        model = str(
            os.environ.get("HQ_SHORT_DRAMA_GROK_MODEL")
            or "grok-imagine-video"
        )
        cost = sum(
            int(points_domain.cost_of("xiaole_video", {
                "channel": "grok", "model": model,
                "resolution": "720p", "duration": duration,
            }))
            for duration in durations
        )
        return {
            "operation_id": operation_id, "runner": runner, "payload": payload,
            "endpoint": endpoint, "kind": "short_drama_preview",
            "cost": cost,
            "avatar_id": int(avatars[0]),
            "reuse_project_id": str((fixture or {}).get("project_id") or ""),
            "parameters": _e2e_parameters(operation_id, payload),
        }
    if operation_id == "short_drama.live_action.delivery":
        fixture = _short_drama_preview_fixture_state(account["username"])
        if not fixture or not fixture.get("all_ready"):
            raise ValueError("短剧正式交付缺少已验收的 30 秒私有预览")
        project_id = str(fixture["project_id"])
        workspace = _content_e2e_get(
            "/api/gen/short-drama/refinement?project_id="
            + urllib.parse.quote(project_id), account_token,
        )
        billing = workspace.get("billing") or {}
        if not (billing.get("deliverable") is True
                and billing.get("mode") == "local_ffmpeg"):
            raise ValueError("短剧 1080p 正式交付执行器尚未准备好")
        current = workspace.get("current_refinement") or {}
        if current.get("issues"):
            raise ValueError("短剧私有预览仍有待处理问题，不能自动确认正式交付")
        return {
            "operation_id": operation_id, "runner": runner, "payload": payload,
            "endpoint": endpoint, "kind": "short_drama_delivery", "cost": 0,
            "reuse_project_id": project_id,
            "parameters": _e2e_parameters(operation_id, payload),
        }
    kind = _e2e_kind(endpoint)
    if not kind:
        raise ValueError("该模式的业务接口尚未接入后台托管测试")
    cost_payload = payload
    if kind == "script_to_video" and payload.get("pipeline") == "pixelle":
        if pixelle_video is None:
            raise RuntimeError("文案成片模块不可用")
        cost_payload = pixelle_video.prepare_payload(payload)
    return {
        "operation_id": operation_id, "runner": runner, "payload": payload,
        "endpoint": endpoint, "kind": kind,
        "cost": _e2e_cost(operation_id, kind, cost_payload, account_token),
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
    _recover_short_drama_unknown(admin_token)
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
        "ready_avatar": True if (
            operation_id.startswith("video.cinematic.")
            or operation_id in {
                "short_drama.live_action.shot_video",
                "short_drama.live_action.preview",
            }
        ) else None,
    }


def _audio_e2e_fixture_attempt():
    with closing(db()) as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS admin_e2e_fixture_attempts(
                   fixture_key TEXT PRIMARY KEY,status TEXT NOT NULL,
                   error TEXT NOT NULL DEFAULT '',updated_at INTEGER NOT NULL)"""
        )
        row = connection.execute(
            "SELECT status,error,updated_at FROM admin_e2e_fixture_attempts WHERE fixture_key='audio.personal'"
        ).fetchone()
        connection.commit()
    return dict(row) if row else None


def _set_audio_e2e_fixture_attempt(status, error=""):
    with closing(db()) as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS admin_e2e_fixture_attempts(
                   fixture_key TEXT PRIMARY KEY,status TEXT NOT NULL,
                   error TEXT NOT NULL DEFAULT '',updated_at INTEGER NOT NULL)"""
        )
        connection.execute(
            """INSERT INTO admin_e2e_fixture_attempts(fixture_key,status,error,updated_at)
               VALUES('audio.personal',?,?,?)
               ON CONFLICT(fixture_key) DO UPDATE SET
                 status=excluded.status,error=excluded.error,updated_at=excluded.updated_at""",
            (status, str(error or "")[:240], int(time.time())),
        )
        connection.commit()


def prepare_audio_e2e_personal_fixture(admin_token):
    """Create the private QA voice once; never return its sample, slot, or voice id."""
    with E2E_FIXTURE_LOCK:
        session = auth_admin_request(
            "/api/auth/admin/e2e/session", admin_token, method="POST", payload={}
        )
        account_token = session["token"]
        try:
            _ready_audio_voice_key("audio.tts.personal", account_token)
            _set_audio_e2e_fixture_attempt("ready")
            return {"ready": True, "state": "ready", "detail": "个人测试音色已就绪"}
        except ValueError:
            pass
        slot_data = _content_e2e_get("/api/gen/audio/slots", account_token)
        slots = slot_data.get("items") or []
        if any(item.get("status") == "training" for item in slots):
            _set_audio_e2e_fixture_attempt("training")
            return {"ready": False, "state": "training", "detail": "个人测试音色正在准备"}
        attempt = _audio_e2e_fixture_attempt() or {}
        if attempt.get("status") in {"unknown", "submitting"}:
            raise ValueError("个人测试音色上次提交结果未知，已禁止重复复刻，请先核对槽位状态")
        if attempt.get("status") == "failed" or any(item.get("status") == "failed" for item in slots):
            _set_audio_e2e_fixture_attempt("failed", "个人测试音色准备失败")
            raise ValueError("个人测试音色准备失败；普通验收不会自动重新复刻")
        slot = next((item for item in slots if item.get("status") == "active"
                     and item.get("slot_id")), None)
        if not slot:
            raise ValueError("专用测试账号没有可用的会员测试音色槽位")
        audio_data = _fixture_data_url(function_registry.QA_VOICE_AUDIO)
        _set_audio_e2e_fixture_attempt("submitting")
        try:
            _content_e2e_post("/api/gen/audio/clone-vip", account_token, {
                "slot_id": slot["slot_id"],
                "name": "后台自动质检音色",
                "audio": audio_data,
                "audio_format": "mp3",
            })
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                http.client.IncompleteRead, http.client.RemoteDisconnected,
                json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            refreshed = _content_e2e_get("/api/gen/audio/slots", account_token)
            if any(item.get("status") in {"training", "ready"}
                   for item in refreshed.get("items") or []):
                _set_audio_e2e_fixture_attempt("training")
                return {"ready": False, "state": "training", "detail": "个人测试音色正在准备"}
            _set_audio_e2e_fixture_attempt("unknown", str(exc))
            raise ValueError("个人测试音色提交结果未知，已禁止重复复刻")
        except ValueError as exc:
            _set_audio_e2e_fixture_attempt("failed", str(exc))
            raise
        _set_audio_e2e_fixture_attempt("training")
        return {"ready": False, "state": "training", "detail": "个人测试音色已开始准备"}


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


def _key_ping_zhipu():
    key = _env_value(["REVERSE_ZHIPU_KEY"])
    if not key:
        return {"ok": False, "error": "密钥未配置", "mode": "auth"}
    base = (_env_value(["REVERSE_ZHIPU_BASE"]) or "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
    return _ping_upstream(
        "GET", base + "/models",
        headers={"Authorization": "Bearer " + key}, proxied=False,
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
    "zhipu": _key_ping_zhipu,
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
    "zhipu": 600,
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
        base = video_minimax_h3.new_task_api_base()
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


def _audio_asset_evidence(job_ids):
    if not job_ids:
        return {}, None
    if not ASSET_DB.exists():
        return {}, "音频资产证据库不存在"
    placeholders = ",".join("?" for _ in job_ids)
    try:
        with closing(sqlite3.connect(str(ASSET_DB), timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """SELECT id,job_id,file,url,asset_kind,metadata_json,created_at
                   FROM audio_assets WHERE job_id IN (%s)""" % placeholders,
                tuple(job_ids),
            ).fetchall()
        evidence = {}
        for row in rows:
            if not row["job_id"]:
                continue
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            evidence[int(row["job_id"])] = {
                "asset_id": int(row["id"]),
                "media_type": "audio",
                "audio_file": row["file"],
                "audio_url": row["url"],
                "asset_kind": row["asset_kind"],
                "provider_request_id": metadata.get("provider_request_id"),
                "phase": "asset_recorded",
                "status": "done",
                "updated_at": int(row["created_at"] or 0),
            }
        return evidence, None
    except sqlite3.Error:
        return {}, "音频资产证据读取失败"


def _verify_local_artifact(evidence):
    media_type = str(evidence.pop("_artifact_media_type", "") or "").strip().lower()
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
        elif media_type == "audio" or path.suffix.lower() in {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a:0",
                 "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=12,
            )
            ok = probe.returncode == 0 and bool(probe.stdout.strip())
            evidence.update({
                "artifact_check": "decodable" if ok else "decode_failed",
                "delivery_verified": ok,
            })
        elif path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
            ok = False
            if PILImage is not None:
                with PILImage.open(path) as image:
                    image.verify()
                    ok = bool(image.format)
            evidence.update({
                "artifact_check": "decodable" if ok else "decode_failed",
                "delivery_verified": ok,
            })
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


def _download_proxy_evidence(job_id, video):
    now = int(time.time())
    with closing(db()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS admin_e2e_delivery_checks(
                job_id INTEGER PRIMARY KEY,status TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',updated_at INTEGER NOT NULL)"""
        )
        cached = connection.execute(
            "SELECT status,detail,updated_at FROM admin_e2e_delivery_checks WHERE job_id=?",
            (int(job_id),),
        ).fetchone()
        fresh_for = 300 if cached and cached["status"] == "checking" else 60
        if cached and (cached["status"] == "passed"
                       or int(cached["updated_at"] or 0) > now - fresh_for):
            connection.commit()
            state = None if cached["status"] == "checking" else cached["status"] == "passed"
            return state, str(cached["detail"] or "")
        connection.execute(
            """INSERT INTO admin_e2e_delivery_checks(job_id,status,detail,updated_at)
               VALUES(?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET
               status=excluded.status,detail=excluded.detail,updated_at=excluded.updated_at""",
            (int(job_id), "checking", "正在通过下载代理验收完整视频", now),
        )
        connection.commit()

    claim_at = now
    status, detail = "failed", "下载代理未返回可用视频"
    try:
        query = urllib.parse.urlencode({
            "url": str(video.get("play_url") or ""),
            "dk": str(video.get("decode_key") or ""),
            "name": str(video.get("title") or "qa-video")[:30],
        })
        request = urllib.request.Request(
            DL_BASE + "/api/gen/dl?" + query,
            headers={"X-HQ-Internal-Token": AUTH_INTERNAL_TOKEN},
        )
        with urllib.request.urlopen(request, timeout=35) as response:
            declared = int(response.headers.get("Content-Length") or 0)
            limit = 200 * 1024 * 1024
            deadline = time.monotonic() + 180
            if declared > limit:
                raise ValueError("验收视频超过 200MB 上限")
            total = 0
            with tempfile.NamedTemporaryFile(suffix=".mp4") as output:
                while True:
                    if time.monotonic() > deadline:
                        raise TimeoutError("完整视频验收超时")
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise ValueError("验收视频超过 200MB 上限")
                    output.write(chunk)
                output.flush()
                if declared and total != declared:
                    raise ValueError("下载内容不完整")
                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=codec_name", "-of", "csv=p=0", output.name],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
                )
        if total and probe.returncode == 0 and probe.stdout.strip():
            status, detail = "passed", "下载代理返回完整视频，文件可解码"
    except Exception as exc:
        detail = ("下载代理验收失败：" + str(exc))[:300]
    with closing(db()) as connection:
        updated = connection.execute(
            """UPDATE admin_e2e_delivery_checks SET status=?,detail=?,updated_at=?
               WHERE job_id=? AND status='checking' AND updated_at=?""",
            (status, detail, int(time.time()), int(job_id), claim_at),
        )
        connection.commit()
        if updated.rowcount == 0:
            current = connection.execute(
                "SELECT status,detail FROM admin_e2e_delivery_checks WHERE job_id=?", (int(job_id),)
            ).fetchone()
            state = None if current and current["status"] == "checking" else bool(
                current and current["status"] == "passed"
            )
            return state, str(current["detail"] or "") if current else "验收状态已更新"
    return status == "passed", detail


def _structured_asset_evidence(row):
    kind = str(row["kind"] or "").lower()
    if kind not in {"collect", "leads", "copy", "breakdown", "canvas_agent"}:
        return None
    try:
        result = json.loads(row["result_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        result = {}
    download_detail = ""
    download_pending = False
    mode = ""
    try:
        request_mode = str(row["request_mode"] or "").lower()
    except (KeyError, IndexError):
        request_mode = ""
    if kind == "leads":
        valid = (result.get("type") == "leads"
                 and bool(result.get("leads"))
                 and int(result.get("leads_count") or 0) > 0)
    elif kind == "collect":
        mode = str(row["collect_mode"] or "comments").lower()
        video, copy = result.get("video") or {}, result.get("copy") or {}
        if mode == "video":
            valid = bool(video.get("play_url"))
            if valid:
                valid, download_detail = _download_proxy_evidence(row["id"], video)
                download_pending = valid is None
        elif mode == "transcript":
            transcript = result.get("transcript") or {}
            valid = bool(transcript.get("text") if isinstance(transcript, dict) else transcript)
        else:
            valid = (result.get("type") == "collect"
                     and bool(video.get("title") or copy.get("title") or copy.get("desc"))
                     and bool(result.get("comments")))
    elif kind == "copy":
        valid = (result.get("type") == "copy"
                 and isinstance(result.get("scenes"), list)
                 and bool(result.get("scenes")))
    elif kind == "breakdown":
        mode = str(result.get("type") or "")
        prompt = result.get("prompt")
        scenes = result.get("scenes")
        valid = bool(
            request_mode == "reverse_prompt"
            and mode == "breakdown_reverse"
            and isinstance(prompt, str)
            and prompt.strip()
        ) if request_mode == "reverse_prompt" else bool(
            mode == "breakdown"
            and isinstance(scenes, list)
            and scenes
            and all(isinstance(scene, dict) and (scene.get("scene") or scene.get("line"))
                    for scene in scenes)
        )
    else:
        plan = result.get("plan") or {}
        actions = plan.get("actions") or []
        draft_modes = {
            action.get("mode") for action in actions
            if isinstance(action, dict)
            and action.get("type") == "create_generation_draft"
        }
        valid = (result.get("type") == "canvas_agent" and isinstance(plan, dict)
                 and bool(str(result.get("content") or plan.get("content") or "").strip())
                 and isinstance(plan.get("actions"), list)
                 and plan.get("requires_confirmation") is True
                 and {"text", "image"}.issubset(draft_modes))
    asset = None
    if kind != "canvas_agent" and ASSET_DB.exists():
        try:
            with closing(sqlite3.connect(str(ASSET_DB), timeout=10)) as connection:
                connection.row_factory = sqlite3.Row
                asset = connection.execute(
                    "SELECT id,kind,stage FROM assets WHERE job_id=? AND kind=? AND deleted=0",
                    (int(row["id"]), kind),
                ).fetchone()
        except sqlite3.Error:
            asset = None
    requires_asset = kind != "canvas_agent"
    return {
        "delivery_verified": bool(valid and (asset or not requires_asset)),
        "artifact_check": ("checking" if download_pending else (
            "download_proxy" if valid and asset and kind == "collect"
                            and str(row["collect_mode"] or "").lower() == "video"
                            else "structured_result" if valid and not requires_asset
                            else "structured_asset") if valid and (asset or not requires_asset) else (
            "invalid_structured" if not valid else "missing"
        )),
        "output_reference_present": bool(valid or download_pending),
        "asset_id": int(asset["id"]) if asset else None,
        "asset_kind": str(asset["kind"]) if asset else None,
        "asset_status": str(asset["stage"]) if asset else None,
        "delivery_detail": download_detail if kind == "collect" and mode == "video" else "",
    }


def _job_evidence(row, asset=None):
    asset = asset or {}
    status = str(row["status"] or "unknown").lower()
    cost = int(row["cost"] or 0)
    refunded = int(row["refunded"] or 0)
    result_url = str(
        row["result_url"] or asset.get("video_url") or asset.get("audio_url") or ""
    ).strip()
    result_file = str(
        row["result_file"] or asset.get("video_file") or asset.get("audio_file") or ""
    ).strip()
    provider_task_id = str(
        asset.get("provider_video_id") or asset.get("provider_request_id")
        or row["provider_result_id"] or ""
    ).strip()
    if provider_task_id.lower() in {"none", "null", "undefined"}:
        provider_task_id = ""
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
    evidence = _verify_local_artifact({
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
        "asset_id": asset.get("asset_id"),
        "asset_kind": asset.get("asset_kind"),
        "_artifact_media_type": asset.get("media_type"),
        "error": str(row["error"] or asset.get("error") or "")[:300],
    })
    if status in {"done", "completed"}:
        structured = _structured_asset_evidence(row)
        if structured is not None:
            evidence.update(structured)
    return evidence


def _e2e_job_evidence(job_id):
    if not job_id or not JOB_DB.exists():
        return None
    try:
        with closing(sqlite3.connect(str(JOB_DB), timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            columns = {item[1] for item in connection.execute("PRAGMA table_info(jobs)")}
            kind_sql = "COALESCE(kind,'')" if "kind" in columns else "''"
            provider_sql = (
                "CASE WHEN json_valid(payload) THEN COALESCE(json_extract(payload,'$.provider'),'') ELSE '' END"
                if "payload" in columns else "''"
            )
            voice_scope_sql = (
                "CASE WHEN json_valid(payload) THEN COALESCE(json_extract(payload,'$.voice_scope'),'') ELSE '' END"
                if "payload" in columns else "''"
            )
            result_sql = "result" if "result" in columns else "NULL"
            collect_mode_sql = (
                "CASE WHEN json_valid(payload) THEN LOWER(COALESCE(json_extract(payload,'$.want[0]'),'comments')) ELSE 'comments' END"
                if "payload" in columns else "'comments'"
            )
            request_mode_sql = (
                "CASE WHEN json_valid(payload) THEN LOWER(COALESCE(json_extract(payload,'$.mode'),'')) ELSE '' END"
                if "payload" in columns else "''"
            )
            row = connection.execute(
                """SELECT id,status,cost,COALESCE(refunded,0) AS refunded,
                          COALESCE(error,'') AS error,created_at,updated_at,
                          CASE WHEN json_valid(result) THEN COALESCE(json_extract(result,'$.video_url'),json_extract(result,'$.image_url'),json_extract(result,'$.url'),json_extract(result,'$.urls[0]'),'') ELSE '' END AS result_url,
                          CASE WHEN json_valid(result) THEN COALESCE(json_extract(result,'$.video_file'),json_extract(result,'$.image_file'),json_extract(result,'$.file'),json_extract(result,'$.files[0]'),'') ELSE '' END AS result_file,
                          CASE WHEN json_valid(result) THEN COALESCE(json_extract(result,'$.provider_task_id'),json_extract(result,'$.request_id'),json_extract(result,'$.provider_video_id'),json_extract(result,'$.video_id'),'') ELSE '' END AS provider_result_id,
                          %s AS kind,%s AS route_provider,%s AS voice_scope,
                          %s AS result_json,%s AS collect_mode,%s AS request_mode
                   FROM jobs WHERE id=?""" % (
                       kind_sql, provider_sql, voice_scope_sql, result_sql,
                       collect_mode_sql, request_mode_sql),
                (int(job_id),),
            ).fetchone()
        if not row:
            return None
        if row["kind"] == "audio":
            assets, _ = _audio_asset_evidence([int(job_id)])
        elif row["kind"] in {"image", "collect", "leads", "copy", "breakdown", "canvas_agent"}:
            assets = {}
        else:
            assets, _ = _video_asset_evidence([int(job_id)])
        asset = dict(assets.get(int(job_id)) or {})
        asset["route_provider"] = row["route_provider"]
        asset["voice_scope"] = row["voice_scope"]
        evidence = _job_evidence(row, asset)
        if row["kind"] in {
            "video", "tryon", "xiaole_video", "sora_video", "cinematic",
            "script_to_video",
        }:
            asset_status = str(asset.get("status") or "").lower()
            asset_consistent = bool(asset and asset_status in {"done", "completed", "succeeded"})
            evidence["asset_consistent"] = asset_consistent
            if evidence.get("completed") and not asset_consistent:
                evidence["delivery_verified"] = False
                evidence["artifact_check"] = "asset_pending" if asset else "asset_missing"
        evidence["route_provider"] = row["route_provider"] or None
        evidence["voice_scope"] = row["voice_scope"] or None
        return evidence
    except (OSError, sqlite3.Error, subprocess.SubprocessError, ValueError):
        return None


def _e2e_stage(key, name, state, detail):
    return {"key": key, "name": name, "state": state, "detail": detail}


def _public_short_drama_shot_run(item, evidence):
    failed = item.get("status") in {"failed", "unknown"}
    provider_job_id = str(evidence.get("provider_job_id") or "")
    provider_task_id = str(evidence.get("provider_task_id") or "")
    provider_status = str(evidence.get("provider_status") or "")
    completed = provider_status == "succeeded"
    delivered = bool(evidence.get("delivery_verified"))
    cleaned = bool(evidence.get("project_cleaned"))
    billing_ok = bool(evidence.get("billing_verified"))
    item["stages"] = [
        _e2e_stage("accepted", "后台测试受理", "passed",
                   "已建立独立测试批次 " + item["run_id"][:8]),
        _e2e_stage("account", "专用账号与点数",
                   "passed" if item.get("username") else "waiting",
                   "专用测试账号已就绪 · 提交前 %s 点" % item.get("points_before")
                   if item.get("username") else "正在取得专用账号"),
        _e2e_stage("job", "业务接口 / provider_job_id",
                   "passed" if provider_job_id else ("failed" if failed else "waiting"),
                   "provider_job_id=" + provider_job_id if provider_job_id
                   else (item.get("error") or "等待业务接口受理")),
        _e2e_stage("route", "渠道与凭据", "passed" if evidence.get("provider") == "grok"
                   else ("failed" if failed else "waiting"),
                   "已选择 Grok 单镜头生产线路" if evidence.get("provider") == "grok"
                   else "等待选择真实生产线路"),
        _e2e_stage("provider", "供应商接单",
                   "passed" if provider_task_id else ("failed" if failed else "waiting"),
                   "provider_task_id=" + provider_task_id if provider_task_id
                   else "等待供应商返回任务编号"),
        _e2e_stage("generation", "单镜头视频生成",
                   "passed" if completed else ("failed" if failed else "waiting"),
                   "供应商已完成 5 秒测试镜头" if completed
                   else (evidence.get("provider_error") or "等待生成终态")),
        _e2e_stage("delivery", "作品交付与清理",
                   "passed" if delivered and cleaned else ("failed" if failed else "waiting"),
                   "视频可解码，临时短剧项目已清理" if delivered and cleaned
                   else ({"decode_failed": "视频返回但无法解码",
                          "missing": "单镜头作品文件缺失"}.get(
                              evidence.get("artifact_check"),
                              "等待视频下载、解码和临时项目清理"))),
        _e2e_stage("billing", "账务闭环",
                   "passed" if billing_ok else ("failed" if failed else "waiting"),
                   "失败任务已退款" if evidence.get("billing_state") == "refunded"
                   else ("扣点流水与余额一致" if billing_ok else "等待扣点或退款证据")),
    ]
    public_evidence = dict(evidence)
    public_evidence.pop("quote_token", None)
    item["evidence"] = public_evidence
    return item


def _public_short_drama_preview_run(item, evidence):
    failed = item.get("status") in {"failed", "unknown"}
    required = list(evidence.get("required_shot_keys") or [])
    ready = list(evidence.get("ready_shot_keys") or [])
    total = len(required) or 6
    ready_count = len(ready)
    preview_job_id = str(evidence.get("preview_job_id") or "")
    preview_done = evidence.get("preview_status") == "succeeded"
    delivered = bool(evidence.get("delivery_verified"))
    billing_ok = bool(evidence.get("billing_verified"))
    item["stages"] = [
        _e2e_stage("accepted", "后台测试受理", "passed",
                   "已建立独立测试批次 " + item["run_id"][:8]),
        _e2e_stage("account", "专用账号与点数",
                   "passed" if item.get("username") else "waiting",
                   "专用测试账号已就绪 · 提交前 %s 点" % item.get("points_before")
                   if item.get("username") else "正在取得专用账号"),
        _e2e_stage("job", "业务项目 / project_id",
                   "passed" if item.get("acceptance_id") else ("failed" if failed else "waiting"),
                   "project_id=" + str(item.get("acceptance_id"))
                   if item.get("acceptance_id") else (item.get("error") or "等待短剧项目受理")),
        _e2e_stage("route", "渠道与凭据",
                   "passed" if evidence.get("provider") == "grok" else ("failed" if failed else "waiting"),
                   "已选择 Grok 逐镜生产线路" if evidence.get("provider") == "grok"
                   else "等待选择真实逐镜线路"),
        _e2e_stage("provider", "供应商逐镜接单",
                   "passed" if ready_count == total else ("failed" if failed else "waiting"),
                   "%s / %s 个镜头已取得真实生产证据" % (ready_count, total)),
        _e2e_stage("generation", "六镜头生成",
                   "passed" if ready_count == total else ("failed" if failed else "waiting"),
                   "%s / %s 个镜头已生成" % (ready_count, total)),
        _e2e_stage("delivery", "720p 预览合成与交付",
                   "passed" if preview_done and delivered else ("failed" if failed else "waiting"),
                   "预览可下载、可解码，私有逐镜快照已保留"
                   if preview_done and delivered else (
                       "preview_job_id=" + preview_job_id if preview_job_id
                       else "等待六镜头齐备后合成")),
        _e2e_stage("billing", "账务闭环",
                   "passed" if billing_ok else ("failed" if failed else "waiting"),
                   "逐镜扣点流水与余额一致" if billing_ok and int(item.get("cost") or 0)
                   else ("复用私有快照，本次合成 0 点" if billing_ok
                         else "等待逐镜扣点或退点证据")),
    ]
    public_evidence = dict(evidence)
    public_evidence.pop("quote_tokens", None)
    public_evidence.pop("provider_task_ids", None)
    item["evidence"] = public_evidence
    return item


def _public_short_drama_delivery_run(item, evidence):
    failed = item.get("status") in {"failed", "unknown"}
    delivery_job_id = str(evidence.get("delivery_job_id") or "")
    completed = evidence.get("delivery_status") == "succeeded"
    delivered = bool(evidence.get("delivery_verified"))
    billing_ok = bool(evidence.get("billing_verified"))
    item["stages"] = [
        _e2e_stage("accepted", "后台测试受理", "passed",
                   "已建立独立测试批次 " + item["run_id"][:8]),
        _e2e_stage("account", "专用账号与点数",
                   "passed" if item.get("username") else "waiting",
                   "专用测试账号已就绪 · 提交前 %s 点" % item.get("points_before")
                   if item.get("username") else "正在取得专用账号"),
        _e2e_stage("job", "业务项目与验收",
                   "passed" if evidence.get("refinement_confirmed")
                   else ("failed" if failed else "waiting"),
                   "六项全片验收已绑定 project_id=" + str(item.get("acceptance_id"))
                   if evidence.get("refinement_confirmed") else "等待确认当前精修版本"),
        _e2e_stage("route", "正式交付执行器",
                   "passed" if evidence.get("capability_verified")
                   else ("failed" if failed else "waiting"),
                   "本机 FFmpeg 1080p 渲染能力已核验"
                   if evidence.get("capability_verified") else "等待渲染能力检查"),
        _e2e_stage("provider", "交付任务接单",
                   "passed" if delivery_job_id else ("failed" if failed else "waiting"),
                   "delivery_job_id=" + delivery_job_id
                   if delivery_job_id else "等待正式交付任务受理"),
        _e2e_stage("generation", "1080p 正式导出",
                   "passed" if completed else ("failed" if failed else "waiting"),
                   "正式交付任务已完成" if completed
                   else (evidence.get("delivery_error") or "等待渲染终态")),
        _e2e_stage("delivery", "作品交付验收",
                   "passed" if delivered else ("failed" if failed else "waiting"),
                   "1920×1080、音视频完整、30 秒且文件可解码"
                   if delivered else "等待正式成片下载与解码验收"),
        _e2e_stage("billing", "账务闭环",
                   "passed" if billing_ok else ("failed" if failed else "waiting"),
                   "0 点报价、任务已关联且测试账号余额未变化"
                   if billing_ok else "等待 0 点账务一致性核对"),
    ]
    public_evidence = dict(evidence)
    public_evidence.pop("quote_token", None)
    item["evidence"] = public_evidence
    return item


def _public_e2e_run(row):
    item = dict(row)
    transaction_key = str(item.pop("transaction_key", "") or "").strip()
    try:
        project_evidence = json.loads(item.pop("evidence_json", "{}") or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        project_evidence = {}
    runner = function_registry.e2e_runner(item.get("operation_id")) or {}
    not_applicable = set(
        ((runner.get("evidence_contract") or {}).get("not_applicable") or [])
    )
    if item.get("operation_id") == "short_drama.live_action.shot_video":
        return _public_short_drama_shot_run(item, project_evidence)
    if item.get("operation_id") == "short_drama.live_action.preview":
        return _public_short_drama_preview_run(item, project_evidence)
    if item.get("operation_id") == "short_drama.live_action.delivery":
        return _public_short_drama_delivery_run(item, project_evidence)
    if (item.get("operation_id") == "short_drama.live_action.script_planning"
            and item.get("acceptance_id") and project_evidence):
        planned = all(project_evidence.get(key) for key in (
            "imported", "direction_confirmed", "script_generated", "script_locked",
            "preflight_ready", "preflight_confirmed",
        ))
        cleaned = bool(project_evidence.get("project_cleaned"))
        zero_point = (
            int(item.get("cost") or 0) == 0
            and item.get("points_before") == item.get("points_after")
        )
        failed = item.get("status") in {"failed", "unknown"}
        item["stages"] = [
            _e2e_stage("accepted", "后台测试受理", "passed",
                       "已建立独立测试批次 " + item["run_id"][:8]),
            _e2e_stage("account", "专用测试账号", "passed" if item.get("username") else "failed",
                       "专用测试账号已就绪" if item.get("username") else "测试账号缺失"),
            _e2e_stage("job", "业务项目 / project_id", "passed",
                       "project_id=" + str(item["acceptance_id"])),
            _e2e_stage("route", "内容服务生产链", "passed",
                       "真实调用短剧导入、编剧对话与制作预检接口"),
            _e2e_stage("provider", "外部供应商任务", "passed",
                       "当前策划旅程不生成媒体，不产生 provider_task_id"),
            _e2e_stage("generation", "剧本与制作计划", "passed" if planned else "failed",
                       "剧本已生成、锁定并完成制作预检" if planned else "多步骤策划证据不完整"),
            _e2e_stage("delivery", "计划验收与清理", "passed" if planned and cleaned else "failed",
                       "制作计划已确认，临时测试项目已清理" if planned and cleaned else "计划确认或测试项目清理未完成"),
            _e2e_stage("billing", "账务闭环", "passed" if zero_point else "failed",
                       "0 点策划旅程，点数未变化" if zero_point else "零点流程出现点数变化"),
        ]
        item["evidence"] = {
            "project_id": item["acceptance_id"], "business_accepted": True,
            "completed": not failed and planned and cleaned,
            "delivery_verified": planned and cleaned,
            "billing_state": "not_applicable", "balance_state": "not_applicable",
            "script_version_id": project_evidence.get("script_version_id"),
            "plan_id": project_evidence.get("plan_id"),
        }
        return item
    evidence = _e2e_job_evidence(item.get("job_id"))
    if evidence:
        job_status = evidence["status"]
        item["status"] = {
            "pending": "queued", "queued": "queued", "running": "running",
            "done": "completed", "completed": "completed",
            "error": "failed", "failed": "failed",
        }.get(job_status, job_status)
        item["error"] = evidence.get("error") or item.get("error") or ""
    if ((project_evidence.get("browser") or {}).get("status") == "running"
            and item.get("status") == "completed"):
        item["status"] = "browser_running"
    character_reference = (
        item.get("operation_id") == "short_drama.live_action.character_reference"
    )
    character_finalized = bool(
        project_evidence.get("reference_locked")
        and project_evidence.get("project_cleaned")
    )
    if (character_reference and evidence
            and evidence.get("status") in {"done", "completed"}
            and not character_finalized):
        item["status"] = "failed" if row["status"] == "failed" else "running"
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
    route_provider = str((evidence or {}).get("route_provider") or "").strip()
    route_label = {
        "cosyvoice": "CosyVoice 音频线路",
        "copy_model": "文案模型线路",
        "tikhub+zhipu": "TikHub 下载 + 智谱视觉分析",
        "tikhub+google": "TikHub 下载 + Gemini 视频分析",
        "local+zhipu": "本地素材 + 智谱视觉分析",
        "local+google": "本地素材 + Gemini 视频分析",
        "openai_responses": "OpenAI Responses 结构化计划",
    }.get(route_provider, route_provider)
    route_ok = bool(provider_id or route_provider)
    provider_not_applicable = "provider_task" in not_applicable
    provider_ok = bool(provider_id or (provider_not_applicable and route_ok))
    completed = bool(evidence and evidence.get("completed"))
    delivered = bool(evidence and evidence.get("delivery_verified"))
    journey_delivered = delivered and (not character_reference or character_finalized)
    delivery_checking = bool(evidence and evidence.get("artifact_check") == "checking")
    refunded = bool(evidence and evidence.get("billing_state") == "refunded")
    asset_consistent = (evidence or {}).get("asset_consistent") is not False
    billing_passed = refunded or (completed and billing_ok and ledger_ok and asset_consistent)
    item["stages"] = [
        _e2e_stage("accepted", "后台测试受理", "passed", "已建立独立测试批次 " + item["run_id"][:8]),
        _e2e_stage("account", "专用账号与点数", "passed" if item.get("username") else "waiting",
                   ("专用测试账号已就绪" if item.get("username") else "正在取得专用账号") + (" · 提交前 %s 点" % item["points_before"] if item.get("points_before") is not None else "")),
        _e2e_stage("job", "业务接口 / job_id", "passed" if item.get("job_id") else ("failed" if failed else "waiting"),
                   "job_id=%s" % item["job_id"] if item.get("job_id") else (item.get("error") or "等待业务接口受理")),
        _e2e_stage("route", "渠道与凭据", "passed" if route_ok else ("failed" if failed else "waiting"),
                   ("任务已选择 " + route_label) if route_provider else (
                       "已进入真实供应商线路" if provider_id else "等待任务选择真实线路；不会用免费连通探针冒充通过")),
        _e2e_stage("provider", "同步生产协议" if provider_not_applicable else "供应商接单",
                   "passed" if provider_ok else ("failed" if failed else "waiting"),
                   (("provider_task_id=" + str(provider_id)) if provider_id else (
                       "同步生产链不产生供应商任务编号，此项不适用" if provider_not_applicable
                       else "尚无供应商任务编号"))),
        _e2e_stage("generation", "角色标准图生成" if character_reference else (
                       "音频生成" if route_provider == "cosyvoice" else (
                       "数据采集" if route_provider == "tikhub" else (
                       "结构化内容生成" if route_provider in {"copy_model", "tikhub+zhipu", "tikhub+google", "openai_responses"}
                       else "供应商生成"))),
                   "passed" if completed else ("failed" if failed else "waiting"),
                   "已到 completed" if completed else (item.get("error") or "等待生成终态")),
        _e2e_stage("delivery", "角色锁定与清理" if character_reference else "作品交付",
                   "passed" if journey_delivered else (
                       "waiting" if delivery_checking or not completed else "failed"),
                   ("图片可解码、角色已锁定、临时项目已清理" if character_reference and journey_delivered else
                    ("图片已生成，正在锁定角色并清理临时项目" if character_reference and delivered else
                    {"decodable": "文件存在且可解码", "file_exists": "文件存在且非空",
                     "structured_asset": "结构化结果已验收并写入资产库",
                     "structured_result": "结构化结果完整且可供客户页面读取",
                     "download_proxy": "下载代理返回完整视频，文件可解码",
                     "asset_missing": "作品文件存在，但尚未写入客户资产库",
                     "asset_pending": "作品文件存在，但客户资产状态尚未完成",
                     "checking": "另一条后台质检正在验收同一视频",
                     "invalid_structured": "结果结构不完整或没有可用结果",
                     "decode_failed": "文件返回但无法解码", "missing": "作品文件缺失",
                     "reference_only": "只有作品地址，尚未完成本地验收"}.get(
                         (evidence or {}).get("artifact_check"), "等待作品文件")))),
        _e2e_stage("billing", "账务闭环", "passed" if billing_passed else ("failed" if failed else "waiting"),
                   "失败任务已退款" if refunded else ("扣点流水一致" if billing_passed else ("点数变化一致，尚未找到扣点流水" if billing_ok else "等待终态扣点 / 退款证据"))),
    ]
    item["evidence"] = dict(evidence or {}, **project_evidence)
    return item


def _public_e2e_rows(rows):
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


def list_e2e_runs(limit=30):
    with closing(db()) as connection:
        rows = connection.execute(
            "SELECT * FROM admin_e2e_runs ORDER BY created_at DESC LIMIT ?",
            (max(1, min(int(limit or 30), 100)),),
        ).fetchall()
    return _public_e2e_rows(rows)


def list_latest_e2e_runs():
    with closing(db()) as connection:
        rows = connection.execute(
            """SELECT current.* FROM admin_e2e_runs AS current
               WHERE NOT EXISTS (
                   SELECT 1 FROM admin_e2e_runs AS newer
                   WHERE newer.operation_id=current.operation_id
                     AND (newer.created_at>current.created_at
                          OR (newer.created_at=current.created_at
                              AND newer.rowid>current.rowid))
               )
               ORDER BY current.created_at DESC,current.rowid DESC"""
        ).fetchall()
    return _public_e2e_rows(rows)


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


def _short_drama_e2e_request(path, account_token, payload, run_id, step):
    key = "e2e:%s:%s" % (run_id, step)
    for attempt in range(2):
        try:
            return _content_e2e_request(
                path, account_token, payload, key, 0, require_job_id=False
            )
        except E2ESubmitUncertain as exc:
            if getattr(exc, "status", 0) == 409:
                raise E2ESubmitRejected(str(exc)) from exc
            if attempt:
                raise


def _e2e_project_evidence(run_id, **values):
    with closing(db()) as connection:
        row = connection.execute(
            "SELECT evidence_json FROM admin_e2e_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        try:
            evidence = json.loads((row[0] if row else "{}") or "{}")
        except (json.JSONDecodeError, TypeError, ValueError):
            evidence = {}
        evidence.update(values)
        connection.execute(
            "UPDATE admin_e2e_runs SET evidence_json=?,updated_at=? WHERE run_id=?",
            (json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
             int(time.time()), run_id),
        )
        connection.commit()
    return evidence


def _browser_job_accepted(run_id, account, endpoint, result):
    job_id = int(result["job_id"])
    actual_cost = int(result["actual_cost"])
    points_after = int(result["points_after"])
    idem = str(result["idempotency_key"])
    transaction_key = "job-charge:%s:%s:%s" % (
        account["username"], endpoint, idem,
    )
    with closing(db()) as connection:
        row = connection.execute(
            "SELECT job_id,evidence_json FROM admin_e2e_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if not row:
            raise ValueError("客户旅程批次不存在")
        if row["job_id"] is not None and int(row["job_id"]) != job_id:
            raise ValueError("同一客户旅程出现两个不同 job_id，已停止验收")
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
        except (json.JSONDecodeError, TypeError, ValueError):
            evidence = {}
        browser = dict(evidence.get("browser") or {})
        browser.update({
            "job_id": job_id,
            "request_sha256": str(result.get("request_sha256") or ""),
        })
        evidence["browser"] = browser
        connection.execute(
            """UPDATE admin_e2e_runs SET username=?,status='queued',job_id=?,cost=?,
                      points_before=?,points_after=?,transaction_key=?,evidence_json=?,updated_at=?
               WHERE run_id=?""",
            (account["username"], job_id, actual_cost, int(account["points"]), points_after,
             transaction_key,
             json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
             int(time.time()), run_id),
        )
        connection.commit()


def _run_browser_e2e(run_id, admin_token, session, prepared):
    account = session["account"]
    contract = prepared["runner"]["browser"]
    endpoint = prepared["endpoint"]

    def refresh_token():
        refreshed = auth_admin_request(
            "/api/auth/admin/e2e/session", admin_token, method="POST", payload={}
        )
        if refreshed["account"]["username"] != account["username"]:
            raise RuntimeError("专用测试账号在客户旅程中发生变化")
        return refreshed["token"]

    try:
        result = browser_qa.run_nb2_reference_journey(
            origin=contract["origin"], account_token=session["token"],
            cookie_name=AUTH_COOKIE_NAME,
            fixture_path=QA_FIXTURE_DIR / contract["fixture"],
            prompt=prepared["payload"]["prompt"], expected_cost=prepared["cost"],
            run_id=run_id, refresh_token=refresh_token,
            on_job=lambda details: _browser_job_accepted(
                run_id, account, endpoint, details
            ),
            timeout_seconds=contract.get("timeout_seconds") or 300,
        )
        _e2e_project_evidence(run_id, browser=result)
    except Exception as exc:
        uncertain = isinstance(exc, browser_qa.BrowserSubmitUncertain)
        with closing(db()) as connection:
            row = connection.execute(
                "SELECT job_id,evidence_json FROM admin_e2e_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            try:
                evidence = json.loads((row["evidence_json"] if row else "{}") or "{}")
            except (json.JSONDecodeError, TypeError, ValueError):
                evidence = {}
            browser = dict(evidence.get("browser") or {})
            browser.update({
                "status": "unknown" if uncertain else "failed",
                "error": str(exc)[:220], "completed_at": int(time.time()),
            })
            evidence["browser"] = browser
            has_job = bool(row and row["job_id"] is not None)
            connection.execute(
                """UPDATE admin_e2e_runs SET status=?,evidence_json=?,error=?,updated_at=?
                   WHERE run_id=?""",
                (("queued" if has_job else ("unknown" if uncertain else "failed")),
                 json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
                 ("" if has_job else (("提交结果未知，禁止自动重试：" if uncertain else "")
                                       + str(exc)[:240])),
                 int(time.time()), run_id),
            )
            connection.commit()


def start_browser_e2e_run(actor, admin_token, operation_id):
    runner = function_registry.e2e_runner(operation_id)
    contract = (runner or {}).get("browser") or {}
    if not runner or not runner.get("supported"):
        raise ValueError((runner or {}).get("blocked_reason") or "测试包尚未准备完成")
    if operation_id != "image.banana.nb2.reference" or not contract.get("supported"):
        raise ValueError("该模式尚未接入客户页自动质检")
    if browser_qa is None:
        raise RuntimeError("服务器缺少客户页质检运行环境")
    with E2E_RUN_LOCK:
        if any(run["status"] in E2E_ACTIVE_STATUSES for run in list_e2e_runs(100)):
            raise ValueError("已有生产链或客户页质检在运行，请等待终态")
        session = auth_admin_request(
            "/api/auth/admin/e2e/session", admin_token, method="POST", payload={}
        )
        account = session["account"]
        prepared = _e2e_prepare_operation(session, operation_id)
        if not account.get("membership_active"):
            raise ValueError("专用测试账号会员未生效")
        if int(account.get("points") or 0) < int(prepared["cost"]):
            raise ValueError("专用测试账号点数不足：需要 %s 点，当前 %s 点" % (
                prepared["cost"], account.get("points") or 0,
            ))
        fixture = (QA_FIXTURE_DIR / contract["fixture"]).resolve()
        if fixture.parent != QA_FIXTURE_DIR.resolve() or not fixture.is_file():
            raise ValueError("客户页私有测试素材未部署")
        run_id = _insert_e2e_run(
            actor, operation_id, status="browser_running",
            username=account["username"], cost=prepared["cost"],
        )
        _e2e_project_evidence(run_id, browser={
            "status": "running", "executor": "playwright", "checks": [],
            "passed": 0, "total": 6, "started_at": int(time.time()),
        })
        thread = threading.Thread(
            target=_run_browser_e2e,
            args=(run_id, admin_token, session, prepared), daemon=True,
            name="admin-browser-e2e-" + run_id[:8],
        )
        try:
            thread.start()
        except Exception as exc:
            with closing(db()) as connection:
                connection.execute(
                    "UPDATE admin_e2e_runs SET status='failed',error=?,updated_at=? WHERE run_id=?",
                    (str(exc)[:240], int(time.time()), run_id),
                )
                connection.commit()
            raise
    return next(run for run in list_e2e_runs(100) if run["run_id"] == run_id)


def _submit_short_drama_character_e2e_run(run_id, admin_token, session, prepared):
    account, token = session["account"], session["token"]
    with closing(db()) as connection:
        row = connection.execute(
            "SELECT acceptance_id,evidence_json,points_before FROM admin_e2e_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        evidence = json.loads((row[1] if row else "{}") or "{}")
        connection.execute(
            """UPDATE admin_e2e_runs SET username=?,status='submitting',cost=?,
                      points_before=COALESCE(points_before,?),updated_at=? WHERE run_id=?""",
            (account["username"], int(prepared["cost"]), int(account["points"]),
             int(time.time()), run_id),
        )
        connection.commit()
    project_id = str((row[0] if row else "") or evidence.get("project_id") or "")
    if not evidence.get("role_saved"):
        imported = _short_drama_e2e_request(
            "/api/gen/short-drama/projects/import", token,
            prepared["payload"], run_id, "import-character-project",
        )
        project_id = str(imported["id"])
        evidence = _e2e_project_evidence(
            run_id, imported=True, project_id=project_id,
        )
        with closing(db()) as connection:
            connection.execute(
                "UPDATE admin_e2e_runs SET acceptance_id=?,updated_at=? WHERE run_id=?",
                (project_id, int(time.time()), run_id),
            )
            connection.commit()
        contract = list(prepared["payload"].get("character_contract") or [])
        saved = _content_e2e_request(
            "/api/gen/short-drama/project?id=" + urllib.parse.quote(project_id),
            token,
            {"revision": int(imported["revision"]),
             "characters": list(imported.get("characters") or []),
             "character_contract": contract},
            "e2e:%s:save-character" % run_id, 0,
            require_job_id=False, method="PUT",
        )
        character = next((item for item in saved.get("characters") or []
                          if item.get("character_key")), None)
        if not character:
            raise ValueError("短剧角色卡保存后没有可生成的角色")
        evidence = _e2e_project_evidence(
            run_id, role_saved=True, project_revision=int(saved["revision"]),
            character_key=str(character["character_key"]),
        )
    idem = "e2e:%s:generate-character" % run_id
    result = _content_e2e_request(
        prepared["endpoint"], token,
        {"project_id": project_id,
         "revision": int(evidence["project_revision"]),
         "character_key": evidence["character_key"]},
        idem, int(prepared["cost"]),
    )
    try:
        job_id = int(result["job_id"])
        points_after = int(result["points_left"])
        digest = hashlib.sha256(
            (account["username"] + "\0" + idem).encode("utf-8")
        ).hexdigest()
        with closing(db()) as connection:
            connection.execute(
                """UPDATE admin_e2e_runs SET status='queued',job_id=?,cost=?,
                          points_after=?,transaction_key=?,error='',updated_at=? WHERE run_id=?""",
                (job_id, int(prepared["cost"]), points_after,
                 "short-drama-character-charge:" + digest,
                 int(time.time()), run_id),
            )
            connection.commit()
    except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
        raise E2ESubmitUncertain("角色标准图已提交，但本地受理证据写入不完整") from exc
    return next(run for run in list_e2e_runs(100) if run["run_id"] == run_id)


def _finalize_short_drama_character_e2e(run_id, admin_token):
    with closing(db()) as connection:
        row = connection.execute(
            "SELECT * FROM admin_e2e_runs WHERE run_id=?", (run_id,)
        ).fetchone()
    if not row or not row["job_id"]:
        return False
    evidence = json.loads(row["evidence_json"] or "{}")
    if evidence.get("project_cleaned"):
        return True
    job = _e2e_job_evidence(row["job_id"])
    if not job or job.get("status") not in {"done", "completed", "error", "failed"}:
        return False
    session = auth_admin_request(
        "/api/auth/admin/e2e/session", admin_token, method="POST", payload={}
    )
    token = session["token"]
    project_id = str(row["acceptance_id"] or evidence.get("project_id") or "")
    try:
        project = _content_e2e_get(
            "/api/gen/short-drama/project?id=" + urllib.parse.quote(project_id), token
        )
    except RuntimeError as exc:
        if evidence.get("imported") and "不存在" in str(exc):
            _e2e_project_evidence(run_id, project_cleaned=True)
            return True
        raise
    if job.get("status") in {"done", "completed"}:
        character = next((item for item in project.get("characters") or []
                          if item.get("character_key") == evidence.get("character_key")), None)
        if not character or not character.get("reference_file") or not character.get("reference_url"):
            raise ValueError("角色标准图完成后没有写回短剧项目")
        if not character.get("reference_locked"):
            project = _short_drama_e2e_request(
                "/api/gen/short-drama/confirm-character-reference", token,
                {"project_id": project_id, "revision": int(project["revision"]),
                 "character_key": character["character_key"],
                 "reference_version": int(character["reference_version"])},
                run_id, "confirm-character-reference",
            )
            character = next(item for item in project["characters"]
                             if item["character_key"] == evidence["character_key"])
        if not character.get("reference_locked"):
            raise ValueError("角色标准图没有锁定")
        evidence = _e2e_project_evidence(
            run_id, reference_locked=True,
            reference_version=int(character["reference_version"]),
        )
    deleted = _short_drama_e2e_request(
        "/api/gen/short-drama/project/delete", token,
        {"project_id": project_id, "revision": int(project["revision"])},
        run_id, "delete-character-project",
    )
    if not deleted.get("deleted"):
        raise ValueError("角色标准图质检项目没有清理")
    _e2e_project_evidence(run_id, project_cleaned=True)
    return True


def _cleanup_short_drama_character_draft(run_id, account_token):
    with closing(db()) as connection:
        row = connection.execute(
            "SELECT acceptance_id,job_id FROM admin_e2e_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
    if not row or row[1] or not row[0]:
        return
    project_id = str(row[0])
    project = _content_e2e_get(
        "/api/gen/short-drama/project?id=" + urllib.parse.quote(project_id),
        account_token,
    )
    deleted = _short_drama_e2e_request(
        "/api/gen/short-drama/projects/live-action/abandon", account_token,
        {"project_id": project_id, "revision": int(project["revision"])},
        run_id, "abandon-character-project",
    )
    _e2e_project_evidence(run_id, project_cleaned=bool(deleted.get("deleted")))


def _resume_short_drama_character_runs(admin_token):
    with closing(db()) as connection:
        run_ids = [row[0] for row in connection.execute(
            """SELECT run_id FROM admin_e2e_runs
               WHERE operation_id='short_drama.live_action.character_reference'
                 AND status IN ('queued','running','failed') AND job_id IS NOT NULL
                 AND COALESCE(json_extract(evidence_json,'$.project_cleaned'),0)=0
               ORDER BY created_at"""
        ).fetchall()]
    for run_id in run_ids:
        try:
            _finalize_short_drama_character_e2e(run_id, admin_token)
        except Exception:
            pass


def _short_drama_shot_state(provider_job_id):
    if not provider_job_id or not JOB_DB.exists():
        return None
    try:
        with closing(sqlite3.connect(str(JOB_DB), timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """SELECT j.*,a.state AS attempt_state,a.charge_key,a.refund_key,
                          v.file AS version_file,v.url AS version_url
                   FROM short_drama_provider_shot_jobs j
                   LEFT JOIN short_drama_provider_shot_attempts a ON a.job_id=j.id
                   LEFT JOIN short_drama_provider_shot_versions v ON v.job_id=j.id
                   WHERE j.id=?""",
                (str(provider_job_id),),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        for key in ("result_json", "error_json"):
            try:
                item[key[:-5]] = json.loads(item.pop(key) or "null")
            except (json.JSONDecodeError, TypeError, ValueError):
                item[key[:-5]] = None
        return item
    except sqlite3.Error:
        return None


def _short_drama_preview_fixture_state(username, project_id=""):
    if not username or not JOB_DB.exists():
        return None
    try:
        with closing(sqlite3.connect(str(JOB_DB), timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            if project_id:
                project = connection.execute(
                    "SELECT id,username,title,revision FROM short_drama_projects "
                    "WHERE id=? AND username=? AND deleted=0",
                    (str(project_id), str(username)),
                ).fetchone()
            else:
                project = connection.execute(
                    "SELECT id,username,title,revision FROM short_drama_projects "
                    "WHERE username=? AND title=? AND deleted=0 "
                    "ORDER BY created_at DESC LIMIT 1",
                    (str(username), "后台质检 · 短剧预览合成"),
                ).fetchone()
            if not project:
                return None
            plan_row = connection.execute(
                "SELECT id,plan_json FROM short_drama_production_plans "
                "WHERE project_id=? AND status='confirmed' "
                "ORDER BY created_at DESC LIMIT 1",
                (project["id"],),
            ).fetchone()
            if not plan_row:
                return None
            plan = json.loads(plan_row["plan_json"] or "{}")
            required = [
                str(item.get("shot_key") or "")
                for item in plan.get("material_plan") or []
                if isinstance(item, dict) and item.get("shot_key")
            ]
            durations = {
                str(item.get("shot_key")): int(item.get("duration_ms") or 0)
                for item in plan.get("material_plan") or []
                if isinstance(item, dict) and item.get("shot_key")
            }
            ready = {}
            for row in connection.execute(
                "SELECT * FROM short_drama_provider_shot_versions "
                "WHERE project_id=? AND status='ready' "
                "ORDER BY shot_key,version DESC,created_at DESC",
                (project["id"],),
            ).fetchall():
                ready.setdefault(str(row["shot_key"]), dict(row))
            latest = {}
            for row in connection.execute(
                """SELECT j.*,a.state AS attempt_state,a.charge_key,a.refund_key
                   FROM short_drama_provider_shot_jobs j
                   LEFT JOIN short_drama_provider_shot_attempts a ON a.job_id=j.id
                   WHERE j.project_id=? ORDER BY j.created_at DESC""",
                (project["id"],),
            ).fetchall():
                latest.setdefault(str(row["shot_key"]), dict(row))
        return {
            "project_id": str(project["id"]), "plan_id": str(plan_row["id"]),
            "required_shot_keys": required,
            "ready_shot_keys": [key for key in required if key in ready],
            "missing_shot_keys": [key for key in required if key not in ready],
            "shot_durations": durations,
            "ready_versions": ready, "latest_jobs": latest,
            "all_ready": bool(required) and all(key in ready for key in required),
        }
    except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError):
        return None


def _short_drama_preview_job_state(project_id, preview_job_id):
    if not project_id or not preview_job_id or not JOB_DB.exists():
        return None
    try:
        with closing(sqlite3.connect(str(JOB_DB), timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM short_drama_autodraft_jobs WHERE id=? AND project_id=?",
                (str(preview_job_id), str(project_id)),
            ).fetchone()
            version = connection.execute(
                "SELECT manifest_json,url,status FROM short_drama_autodraft_versions "
                "WHERE job_id=? ORDER BY version DESC LIMIT 1",
                (str(preview_job_id),),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["request"] = json.loads(item.pop("request_json") or "{}")
        item["result"] = json.loads(item.pop("result_json") or "null")
        item["error"] = json.loads(item.pop("error_json") or "null")
        if version:
            item["version_status"] = str(version["status"] or "")
            item["version_url"] = str(version["url"] or "")
            item["manifest"] = json.loads(version["manifest_json"] or "{}")
        return item
    except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError):
        return None


def _short_drama_delivery_attempt_state(username, project_id, job_id):
    if not username or not project_id or not job_id or not JOB_DB.exists():
        return None
    try:
        with closing(sqlite3.connect(str(JOB_DB), timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """SELECT a.state AS attempt_state,a.cost AS attempt_cost,
                          j.status AS job_status,j.cost AS job_cost
                   FROM short_drama_delivery_jobs j
                   JOIN short_drama_projects p ON p.id=j.project_id
                   LEFT JOIN short_drama_delivery_attempts a ON a.job_id=j.id
                   WHERE j.id=? AND j.project_id=? AND p.username=?""",
                (str(job_id), str(project_id), str(username)),
            ).fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        return None


def _delete_short_drama_shot_project(run_id, account_token):
    with closing(db()) as connection:
        row = connection.execute(
            "SELECT acceptance_id,evidence_json FROM admin_e2e_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
    if not row or not row[0]:
        return False
    evidence = json.loads(row[1] or "{}")
    if evidence.get("project_cleaned"):
        return True
    project_id = str(row[0])
    try:
        project = _content_e2e_get(
            "/api/gen/short-drama/project?id=" + urllib.parse.quote(project_id),
            account_token,
        )
    except RuntimeError as exc:
        if "不存在" in str(exc):
            _e2e_project_evidence(run_id, project_cleaned=True)
            return True
        raise
    deleted = _short_drama_e2e_request(
        "/api/gen/short-drama/project/delete", account_token,
        {"project_id": project_id, "revision": int(project["revision"])},
        run_id, "delete-shot-project",
    )
    cleaned = bool(deleted.get("deleted"))
    _e2e_project_evidence(run_id, project_cleaned=cleaned)
    return cleaned


def _submit_short_drama_shot_e2e_run(run_id, admin_token, session, prepared):
    account, token = session["account"], session["token"]
    with closing(db()) as connection:
        connection.execute(
            """UPDATE admin_e2e_runs SET username=?,status='submitting',cost=?,
                      points_before=COALESCE(points_before,?),updated_at=? WHERE run_id=?""",
            (account["username"], int(prepared["cost"]), int(account["points"]),
             int(time.time()), run_id),
        )
        connection.commit()
    imported = _short_drama_e2e_request(
        "/api/gen/short-drama/projects/import", token,
        prepared["payload"], run_id, "import-shot-project",
    )
    project_id = str(imported["id"])
    _e2e_project_evidence(run_id, imported=True, project_id=project_id)
    with closing(db()) as connection:
        connection.execute(
            "UPDATE admin_e2e_runs SET acceptance_id=?,updated_at=? WHERE run_id=?",
            (project_id, int(time.time()), run_id),
        )
        connection.commit()
    saved = _content_e2e_request(
        "/api/gen/short-drama/project?id=" + urllib.parse.quote(project_id),
        token,
        {"revision": int(imported["revision"]),
         "characters": list(imported.get("characters") or []),
         "character_contract": list(
             prepared["payload"].get("character_contract") or []
         )},
        "e2e:%s:save-shot-character" % run_id, 0,
        require_job_id=False, method="PUT",
    )
    if not any(item.get("character_key") for item in saved.get("characters") or []):
        raise ValueError("短剧角色卡保存后没有可绑定角色")
    confirmed = _short_drama_e2e_request(
        "/api/gen/short-drama/conversation/messages", token,
        {"project_id": project_id, "conversation_revision": 1,
         "message": "确认尊重原稿并生成"}, run_id, "confirm-shot-direction",
    )
    conversation = confirmed["conversation"]
    if not (conversation.get("understanding") or {}).get("direction_confirmed"):
        raise ValueError("短剧原稿方向没有确认")
    generated = _short_drama_e2e_request(
        "/api/gen/short-drama/conversation/script/generate", token,
        {"project_id": project_id,
         "conversation_revision": int(conversation["revision"]),
         "instruction": "尊重原稿"}, run_id, "generate-shot-script",
    )
    locked = _short_drama_e2e_request(
        "/api/gen/short-drama/conversation/script/lock", token,
        {"project_id": project_id,
         "conversation_revision": int(generated["conversation"]["revision"]),
         "version_id": generated["current_script"]["id"]},
        run_id, "lock-shot-script",
    )
    if (locked.get("conversation") or {}).get("state") != "script_locked":
        raise ValueError("短剧剧本没有锁定")
    project = _content_e2e_get(
        "/api/gen/short-drama/project?id=" + urllib.parse.quote(project_id), token
    )
    character = next((item for item in project.get("characters") or []
                      if item.get("character_key")), None)
    if not character:
        raise ValueError("短剧剧本没有可绑定的角色")
    bound = _short_drama_e2e_request(
        "/api/gen/short-drama/character-studio/bind-avatar", token,
        {"project_id": project_id, "project_revision": int(project["revision"]),
         "character_key": character["character_key"],
         "avatar_id": str(prepared["avatar_id"])},
        run_id, "bind-shot-avatar",
    )
    prepared_plan = _short_drama_e2e_request(
        "/api/gen/short-drama/preflight/generate", token,
        {"project_id": project_id,
         "conversation_revision": int(locked["conversation"]["revision"]),
         "quality_route": "quick_draft"}, run_id, "generate-shot-preflight",
    )
    current_plan = prepared_plan["current_plan"]
    plan = current_plan["plan"]
    accepted = _short_drama_e2e_request(
        "/api/gen/short-drama/preflight/confirm", token,
        {"project_id": project_id, "plan_id": current_plan["id"],
         "plan_version": int(current_plan["version"]),
         "accepted_issue_keys": list(plan.get("required_acceptance") or [])},
        run_id, "confirm-shot-preflight",
    )
    if (accepted.get("current_plan") or {}).get("status") != "confirmed":
        raise ValueError("短剧制作计划没有确认")
    character_key = str(character["character_key"])
    shot = next((item for item in plan.get("material_plan") or []
                 if int(item.get("duration_ms") or 0) == 5000
                 and character_key in list(item.get("character_keys") or [])), None)
    if not shot:
        raise ValueError("固定短剧没有找到绑定角色的 5 秒测试镜头")
    request = {
        "project_id": project_id, "plan_id": str(current_plan["id"]),
        "shot_key": str(shot["shot_key"]), "character_key": character_key,
        "avatar_id": str(prepared["avatar_id"]),
    }
    preview = _short_drama_e2e_request(
        "/api/gen/short-drama/autodraft/provider-preflight", token,
        request, run_id, "provider-shot-preflight",
    )
    if (not preview.get("ready") or preview.get("provider") != "grok"
            or int((preview.get("request") or {}).get("duration_seconds") or 0) != 5):
        raise ValueError("短剧单镜头真实 Provider 预检没有通过")
    quote = _short_drama_e2e_request(
        "/api/gen/short-drama/autodraft/provider-quote", token,
        request, run_id, "provider-shot-quote",
    )
    if int(quote.get("cost") or 0) != int(prepared["cost"]):
        raise ValueError("单镜头实时价格已变化，本次在扣点前安全停止")
    evidence = _e2e_project_evidence(
        run_id, direction_confirmed=True, script_locked=True,
        avatar_bound=bool(bound.get("binding_ready", True)),
        character_key=character_key, plan_id=str(current_plan["id"]),
        shot_key=str(shot["shot_key"]), provider="grok",
        quote_token=str(quote["quote_token"]), quote_cost=int(quote["cost"]),
    )
    result = _short_drama_e2e_request(
        "/api/gen/short-drama/autodraft/provider-jobs", token,
        {"project_id": project_id, "quote_token": quote["quote_token"]},
        run_id, "provider-shot-submit",
    )
    provider_job_id = str(result.get("id") or "")
    if not provider_job_id:
        raise E2ESubmitUncertain("单镜头业务接口响应缺少 provider_job_id")
    state = _short_drama_shot_state(provider_job_id) or {}
    refreshed = auth_admin_request(
        "/api/auth/admin/e2e/session", admin_token, method="POST", payload={}
    )
    evidence.update({
        "provider_job_id": provider_job_id,
        "provider_status": str(result.get("status") or "queued"),
        "provider_task_id": result.get("provider_job_id") or None,
    })
    with closing(db()) as connection:
        connection.execute(
            """UPDATE admin_e2e_runs SET status='queued',points_after=?,
                      transaction_key=?,evidence_json=?,error='',updated_at=? WHERE run_id=?""",
            (int(refreshed["account"]["points"]), str(state.get("charge_key") or ""),
             json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
             int(time.time()), run_id),
        )
        connection.commit()
    return next(run for run in list_e2e_runs(100) if run["run_id"] == run_id)


def _finalize_short_drama_shot_e2e(run_id, admin_token):
    with closing(db()) as connection:
        row = connection.execute(
            "SELECT * FROM admin_e2e_runs WHERE run_id=?", (run_id,)
        ).fetchone()
    if not row:
        return False
    evidence = json.loads(row["evidence_json"] or "{}")
    provider_job_id = str(evidence.get("provider_job_id") or "")
    if not provider_job_id:
        return False
    session = auth_admin_request(
        "/api/auth/admin/e2e/session", admin_token, method="POST", payload={}
    )
    token = session["token"]
    project_id = str(row["acceptance_id"] or evidence.get("project_id") or "")
    result = _content_e2e_get(
        "/api/gen/short-drama/autodraft/provider-jobs/"
        + urllib.parse.quote(provider_job_id)
        + "?project_id=" + urllib.parse.quote(project_id),
        token,
    )
    state = _short_drama_shot_state(provider_job_id) or {}
    status = str(result.get("status") or state.get("status") or "unknown")
    error = result.get("error") or state.get("error") or {}
    evidence.update({
        "provider_status": status,
        "provider_task_id": result.get("provider_job_id") or state.get("provider_job_id"),
        "provider_error": str(
            error.get("detail") if isinstance(error, dict) else error or ""
        )[:220],
    })
    if status not in {"succeeded", "failed", "canceled", "submit_unknown"}:
        with closing(db()) as connection:
            connection.execute(
                """UPDATE admin_e2e_runs SET status='running',evidence_json=?,
                          transaction_key=?,updated_at=? WHERE run_id=?""",
                (json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
                 str(state.get("charge_key") or row["transaction_key"] or ""),
                 int(time.time()), run_id),
            )
            connection.commit()
        return False
    if status == "submit_unknown":
        with closing(db()) as connection:
            connection.execute(
                """UPDATE admin_e2e_runs SET status='unknown',evidence_json=?,error=?,
                          updated_at=? WHERE run_id=?""",
                (json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
                 "供应商提交结果未知，禁止自动重试", int(time.time()), run_id),
            )
            connection.commit()
        return False
    attempt_state = str(state.get("attempt_state") or "")
    if status == "failed" and attempt_state == "refund_pending":
        _e2e_project_evidence(
            run_id, provider_status=status, billing_state="refund_pending",
            provider_error=evidence.get("provider_error") or "单镜头生成失败，等待退点",
        )
        return False
    artifact = _verify_local_artifact({
        "result_file": str(
            (result.get("result") or {}).get("file")
            or state.get("version_file") or ""
        ),
        "result_url": str(
            (result.get("result") or {}).get("url")
            or state.get("version_url") or ""
        ),
        "_artifact_media_type": "video",
    })
    points_before = int(row["points_before"] or 0)
    refreshed = auth_admin_request(
        "/api/auth/admin/e2e/session", admin_token, method="POST", payload={}
    )
    points_after = int(refreshed["account"]["points"])
    cost = int(row["cost"] or 0)
    charge_key = str(state.get("charge_key") or row["transaction_key"] or "")
    refund_key = str(state.get("refund_key") or "")
    get_transaction = getattr(points_domain, "get_points_transaction", None)
    charge = get_transaction(charge_key) if charge_key and callable(get_transaction) else None
    refund = get_transaction(refund_key) if refund_key and callable(get_transaction) else None
    charge_ok = bool(
        charge and str(charge.get("username") or "") == str(row["username"] or "")
        and int(charge.get("delta") or 0) == -cost
    )
    refunded = attempt_state == "refunded"
    refund_ok = bool(
        refunded and refund
        and str(refund.get("username") or "") == str(row["username"] or "")
        and int(refund.get("delta") or 0) == cost
        and points_after == points_before
    )
    billing_ok = (
        status == "succeeded" and charge_ok and points_before - points_after == cost
    ) or (status in {"failed", "canceled"} and (
        refund_ok or (not charge and not cost and points_after == points_before)
    ))
    evidence.update({
        "result_file": artifact.get("result_file") or None,
        "result_url": artifact.get("result_url") or None,
        "artifact_check": artifact.get("artifact_check") or "missing",
        "delivery_verified": bool(artifact.get("delivery_verified")),
        "billing_state": "refunded" if refunded else (
            "charged" if charge_ok else "unverified"
        ),
        "billing_verified": billing_ok,
    })
    cleaned = _delete_short_drama_shot_project(run_id, token)
    evidence = _e2e_project_evidence(run_id, **evidence, project_cleaned=cleaned)
    passed = bool(
        status == "succeeded" and evidence.get("delivery_verified")
        and billing_ok and cleaned
    )
    message = "" if passed else (
        evidence.get("provider_error") or
        ("单镜头作品无法解码" if not evidence.get("delivery_verified") else
         "单镜头账务或临时项目清理未闭环")
    )
    with closing(db()) as connection:
        connection.execute(
            """UPDATE admin_e2e_runs SET status=?,points_after=?,transaction_key=?,
                      evidence_json=?,error=?,updated_at=? WHERE run_id=?""",
            ("completed" if passed else "failed", points_after, charge_key,
             json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
             str(message)[:300], int(time.time()), run_id),
        )
        connection.commit()
    return True


def _resume_short_drama_shot_runs(admin_token):
    with closing(db()) as connection:
        rows = connection.execute(
            """SELECT run_id,evidence_json FROM admin_e2e_runs
               WHERE operation_id='short_drama.live_action.shot_video'
                 AND status IN ('queued','running','unknown')
               ORDER BY created_at"""
        ).fetchall()
    for run_id, raw_evidence in rows:
        try:
            evidence = json.loads(raw_evidence or "{}")
            if not evidence.get("provider_job_id") and evidence.get("quote_token"):
                session = auth_admin_request(
                    "/api/auth/admin/e2e/session", admin_token,
                    method="POST", payload={},
                )
                result = _short_drama_e2e_request(
                    "/api/gen/short-drama/autodraft/provider-jobs", session["token"],
                    {"project_id": evidence["project_id"],
                     "quote_token": evidence["quote_token"]},
                    run_id, "provider-shot-submit",
                )
                provider_job_id = str(result.get("id") or "")
                if not provider_job_id:
                    continue
                _e2e_project_evidence(
                    run_id, provider_job_id=provider_job_id,
                    provider_status=str(result.get("status") or "queued"),
                )
                with closing(db()) as connection:
                    connection.execute(
                        "UPDATE admin_e2e_runs SET status='queued',error='',updated_at=? WHERE run_id=?",
                        (int(time.time()), run_id),
                    )
                    connection.commit()
            _finalize_short_drama_shot_e2e(run_id, admin_token)
        except Exception:
            pass


def _prepare_short_drama_preview_project(run_id, token, prepared):
    imported = _short_drama_e2e_request(
        "/api/gen/short-drama/projects/import", token,
        prepared["payload"], run_id, "import-preview-project",
    )
    project_id = str(imported["id"])
    with closing(db()) as connection:
        connection.execute(
            "UPDATE admin_e2e_runs SET acceptance_id=?,updated_at=? WHERE run_id=?",
            (project_id, int(time.time()), run_id),
        )
        connection.commit()
    saved = _content_e2e_request(
        "/api/gen/short-drama/project?id=" + urllib.parse.quote(project_id),
        token,
        {"revision": int(imported["revision"]),
         "characters": list(imported.get("characters") or []),
         "character_contract": list(
             prepared["payload"].get("character_contract") or []
         )},
        "e2e:%s:save-preview-character" % run_id, 0,
        require_job_id=False, method="PUT",
    )
    character = next((item for item in saved.get("characters") or []
                      if item.get("character_key")), None)
    if not character:
        raise ValueError("短剧预览角色卡保存失败")
    confirmed = _short_drama_e2e_request(
        "/api/gen/short-drama/conversation/messages", token,
        {"project_id": project_id, "conversation_revision": 1,
         "message": "确认尊重原稿并生成"}, run_id, "confirm-preview-direction",
    )
    conversation = confirmed["conversation"]
    if not (conversation.get("understanding") or {}).get("direction_confirmed"):
        raise ValueError("短剧预览原稿方向没有确认")
    generated = _short_drama_e2e_request(
        "/api/gen/short-drama/conversation/script/generate", token,
        {"project_id": project_id,
         "conversation_revision": int(conversation["revision"]),
         "instruction": "尊重原稿"}, run_id, "generate-preview-script",
    )
    locked = _short_drama_e2e_request(
        "/api/gen/short-drama/conversation/script/lock", token,
        {"project_id": project_id,
         "conversation_revision": int(generated["conversation"]["revision"]),
         "version_id": generated["current_script"]["id"]},
        run_id, "lock-preview-script",
    )
    if (locked.get("conversation") or {}).get("state") != "script_locked":
        raise ValueError("短剧预览剧本没有锁定")
    project = _content_e2e_get(
        "/api/gen/short-drama/project?id=" + urllib.parse.quote(project_id), token
    )
    characters = [item for item in project.get("characters") or []
                  if item.get("character_key")]
    if len(characters) != 1:
        raise ValueError("短剧预览固定素材必须只包含 1 个可绑定角色")
    bound = _short_drama_e2e_request(
        "/api/gen/short-drama/character-studio/bind-avatar", token,
        {"project_id": project_id, "project_revision": int(project["revision"]),
         "character_key": characters[0]["character_key"],
         "avatar_id": str(prepared["avatar_id"])},
        run_id, "bind-preview-avatar",
    )
    prepared_plan = _short_drama_e2e_request(
        "/api/gen/short-drama/preflight/generate", token,
        {"project_id": project_id,
         "conversation_revision": int(locked["conversation"]["revision"]),
         "quality_route": "quick_draft"}, run_id, "generate-preview-preflight",
    )
    current_plan = prepared_plan["current_plan"]
    plan = current_plan["plan"]
    accepted = _short_drama_e2e_request(
        "/api/gen/short-drama/preflight/confirm", token,
        {"project_id": project_id, "plan_id": current_plan["id"],
         "plan_version": int(current_plan["version"]),
         "accepted_issue_keys": list(plan.get("required_acceptance") or [])},
        run_id, "confirm-preview-preflight",
    )
    if (accepted.get("current_plan") or {}).get("status") != "confirmed":
        raise ValueError("短剧预览制作计划没有确认")
    shots = [item for item in plan.get("material_plan") or []
             if isinstance(item, dict) and item.get("shot_key")]
    durations = [int(item.get("duration_ms") or 0) for item in shots]
    if len(shots) != 6 or sum(durations) != 30000 or any(
        duration <= 0 or duration % 1000 for duration in durations
    ):
        raise ValueError("固定预览必须生成 6 个合计 30 秒的整秒镜头")
    _e2e_project_evidence(
        run_id, project_id=project_id, plan_id=str(current_plan["id"]),
        character_key=str(characters[0]["character_key"]),
        avatar_bound=bool(bound.get("ok", bound.get("binding_ready", True))),
        required_shot_keys=[str(item["shot_key"]) for item in shots],
        provider_jobs={}, quote_tokens={}, submitted_job_ids=[],
    )
    return _short_drama_preview_fixture_state(
        prepared["account_username"], project_id
    )


def _preview_e2e_row(run_id):
    with closing(db()) as connection:
        row = connection.execute(
            "SELECT * FROM admin_e2e_runs WHERE run_id=?", (run_id,)
        ).fetchone()
    if not row:
        return None, {}
    try:
        evidence = json.loads(row["evidence_json"] or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        evidence = {}
    return row, evidence


def _preview_submit_shot(run_id, token, evidence, shot_key):
    project_id = str(evidence["project_id"])
    request = {
        "project_id": project_id, "plan_id": str(evidence["plan_id"]),
        "shot_key": str(shot_key),
    }
    quote_tokens = dict(evidence.get("quote_tokens") or {})
    quote_token = str(quote_tokens.get(shot_key) or "")
    if not quote_token:
        preview = _short_drama_e2e_request(
            "/api/gen/short-drama/autodraft/provider-preflight", token,
            request, run_id, "preview-shot-preflight-" + str(shot_key),
        )
        if (not preview.get("ready") or preview.get("provider") != "grok"
                or int((preview.get("request") or {}).get("duration_seconds") or 0) <= 0):
            raise ValueError("短剧预览镜头 %s 真实 Provider 预检失败" % shot_key)
        quote = _short_drama_e2e_request(
            "/api/gen/short-drama/autodraft/provider-quote", token,
            request, run_id, "preview-shot-quote-" + str(shot_key),
        )
        provider_request = preview.get("request") or {}
        expected_cost = int(points_domain.cost_of("xiaole_video", {
            "channel": "grok",
            "model": str(provider_request.get("model") or "grok-imagine-video"),
            "resolution": str(provider_request.get("resolution") or "720p"),
            "duration": int(provider_request.get("duration_seconds") or 0),
        }))
        if int(quote.get("cost") or 0) != expected_cost:
            raise ValueError("逐镜实时价格已变化，本次在新镜头扣点前停止")
        quote_token = str(quote["quote_token"])
        quote_tokens[str(shot_key)] = quote_token
        evidence = _e2e_project_evidence(run_id, quote_tokens=quote_tokens)
    result = _short_drama_e2e_request(
        "/api/gen/short-drama/autodraft/provider-jobs", token,
        {"project_id": project_id, "quote_token": quote_token},
        run_id, "preview-shot-submit-" + str(shot_key),
    )
    provider_job_id = str(result.get("id") or "")
    if not provider_job_id:
        raise E2ESubmitUncertain("短剧预览逐镜响应缺少 provider_job_id")
    provider_jobs = dict(evidence.get("provider_jobs") or {})
    provider_jobs[str(shot_key)] = provider_job_id
    submitted = list(dict.fromkeys(
        list(evidence.get("submitted_job_ids") or []) + [provider_job_id]
    ))
    return _e2e_project_evidence(
        run_id, provider_jobs=provider_jobs, submitted_job_ids=submitted,
    )


def _preview_billing_verified(row, evidence, points_after):
    job_ids = [str(item) for item in evidence.get("submitted_job_ids") or []]
    if not job_ids:
        return int(row["cost"] or 0) == 0 and int(row["points_before"] or 0) == points_after
    get_transaction = getattr(points_domain, "get_points_transaction", None)
    if not callable(get_transaction):
        return False
    charged = 0
    for job_id in job_ids:
        state = _short_drama_shot_state(job_id) or {}
        if state.get("attempt_state") != "done":
            return False
        cost = int(state.get("cost") or 0)
        transaction = get_transaction(str(state.get("charge_key") or ""))
        if not transaction or int(transaction.get("delta") or 0) != -cost:
            return False
        charged += cost
    return (
        charged == int(row["cost"] or 0)
        and int(row["points_before"] or 0) - points_after == charged
    )


def _finalize_short_drama_preview_failure(run_id, admin_token, message):
    row, evidence = _preview_e2e_row(run_id)
    if not row:
        return False
    session = auth_admin_request(
        "/api/auth/admin/e2e/session", admin_token, method="POST", payload={}
    )
    points_after = int(session["account"]["points"])
    pending_refund = any(
        (_short_drama_shot_state(job_id) or {}).get("attempt_state") == "refund_pending"
        for job_id in evidence.get("submitted_job_ids") or []
    )
    evidence.update({
        "billing_state": "refund_pending" if pending_refund else "partial",
        "billing_verified": False,
    })
    with closing(db()) as connection:
        connection.execute(
            "UPDATE admin_e2e_runs SET status=?,points_after=?,evidence_json=?,error=?,updated_at=? WHERE run_id=?",
            ("running" if pending_refund else "failed", points_after,
             json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
             str(message)[:300], int(time.time()), run_id),
        )
        connection.commit()
    return not pending_refund


def _advance_short_drama_preview_e2e(run_id, admin_token):
    row, evidence = _preview_e2e_row(run_id)
    if not row:
        return False
    session = auth_admin_request(
        "/api/auth/admin/e2e/session", admin_token, method="POST", payload={}
    )
    token = session["token"]
    project_id = str(row["acceptance_id"] or evidence.get("project_id") or "")
    provider_jobs = dict(evidence.get("provider_jobs") or {})
    for shot_key, provider_job_id in provider_jobs.items():
        state = _short_drama_shot_state(provider_job_id) or {}
        if str(state.get("status") or "") in {
            "billing", "queued", "submitting", "running",
        }:
            result = _content_e2e_get(
                "/api/gen/short-drama/autodraft/provider-jobs/"
                + urllib.parse.quote(str(provider_job_id))
                + "?project_id=" + urllib.parse.quote(project_id), token,
            )
            if result.get("provider_job_id"):
                provider_task_ids = dict(evidence.get("provider_task_ids") or {})
                provider_task_ids[str(shot_key)] = str(result["provider_job_id"])
                evidence["provider_task_ids"] = provider_task_ids
    state = _short_drama_preview_fixture_state(row["username"], project_id)
    if not state:
        return _finalize_short_drama_preview_failure(
            run_id, admin_token, "短剧预览私有素材项目不存在"
        )
    missing = list(state.get("missing_shot_keys") or [])
    latest = state.get("latest_jobs") or {}
    submitted_ids = set(str(item) for item in evidence.get("submitted_job_ids") or [])
    failed = [key for key in missing
              if str((latest.get(key) or {}).get("id") or "") in submitted_ids
              and str((latest.get(key) or {}).get("status") or "")
              in {"failed", "canceled", "submit_unknown"}]
    if failed:
        detail = (latest.get(failed[0]) or {}).get("error_json") or ""
        return _finalize_short_drama_preview_failure(
            run_id, admin_token,
            "短剧预览镜头 %s 生成失败：%s" % (failed[0], str(detail)[:180]),
        )
    active = sum(
        str((latest.get(key) or {}).get("status") or "")
        in {"billing", "queued", "submitting", "running"}
        for key in missing
    )
    for shot_key in missing:
        if active >= 2:
            break
        current = latest.get(shot_key) or {}
        if str(current.get("status") or "") in {
            "billing", "queued", "submitting", "running",
        }:
            continue
        evidence = _preview_submit_shot(
            run_id, token, evidence, shot_key,
        )
        active += 1
    state = _short_drama_preview_fixture_state(row["username"], project_id) or state
    evidence = _e2e_project_evidence(
        run_id, provider="grok",
        ready_shot_keys=list(state.get("ready_shot_keys") or []),
        missing_shot_keys=list(state.get("missing_shot_keys") or []),
        provider_task_ids=evidence.get("provider_task_ids") or {},
    )
    if not state.get("all_ready"):
        with closing(db()) as connection:
            connection.execute(
                "UPDATE admin_e2e_runs SET status='running',evidence_json=?,error='',updated_at=? WHERE run_id=?",
                (json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
                 int(time.time()), run_id),
            )
            connection.commit()
        return False
    preview_job_id = str(evidence.get("preview_job_id") or "")
    if not preview_job_id:
        result = _short_drama_e2e_request(
            "/api/gen/short-drama/autodraft/jobs", token,
            {"project_id": project_id, "plan_id": str(state["plan_id"])},
            run_id, "submit-preview-assembly",
        )
        preview_job_id = str(result.get("id") or "")
        if not preview_job_id:
            raise E2ESubmitUncertain("短剧预览合成响应缺少 job_id")
        evidence = _e2e_project_evidence(
            run_id, preview_job_id=preview_job_id,
            preview_status=str(result.get("status") or "queued"),
        )
    result = _content_e2e_get(
        "/api/gen/short-drama/autodraft/jobs/"
        + urllib.parse.quote(preview_job_id)
        + "?project_id=" + urllib.parse.quote(project_id), token,
    )
    preview = _short_drama_preview_job_state(project_id, preview_job_id) or {}
    status = str(result.get("status") or preview.get("status") or "unknown")
    evidence = _e2e_project_evidence(
        run_id, preview_status=status,
        preview_version_id=(result.get("result") or {}).get("version_id"),
    )
    if status not in {"succeeded", "failed", "canceled"}:
        with closing(db()) as connection:
            connection.execute(
                "UPDATE admin_e2e_runs SET status='running',error='',updated_at=? WHERE run_id=?",
                (int(time.time()), run_id),
            )
            connection.commit()
        return False
    if status != "succeeded":
        error = result.get("error") or preview.get("error") or {}
        return _finalize_short_drama_preview_failure(
            run_id, admin_token,
            str(error.get("detail") if isinstance(error, dict) else error or "短剧预览合成失败"),
        )
    manifest = preview.get("manifest") or {}
    artifact = _verify_local_artifact({
        "result_file": str(manifest.get("playback_file") or ""),
        "result_url": str(manifest.get("playback_url") or preview.get("version_url") or ""),
        "_artifact_media_type": "video",
    })
    points_after = int(auth_admin_request(
        "/api/auth/admin/e2e/session", admin_token, method="POST", payload={}
    )["account"]["points"])
    billing_ok = _preview_billing_verified(row, evidence, points_after)
    evidence.update({
        "result_file": artifact.get("result_file") or None,
        "result_url": artifact.get("result_url") or None,
        "artifact_check": artifact.get("artifact_check") or "missing",
        "delivery_verified": bool(artifact.get("delivery_verified")),
        "billing_state": "charged" if int(row["cost"] or 0) else "fixture_reused",
        "billing_verified": billing_ok, "fixture_retained": True,
    })
    passed = bool(evidence["delivery_verified"] and billing_ok)
    with closing(db()) as connection:
        connection.execute(
            "UPDATE admin_e2e_runs SET status=?,points_after=?,evidence_json=?,error=?,updated_at=? WHERE run_id=?",
            ("completed" if passed else "failed", points_after,
             json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
             "" if passed else "短剧预览文件或账务证据未闭环",
             int(time.time()), run_id),
        )
        connection.commit()
    return True


def _submit_short_drama_preview_e2e_run(run_id, admin_token, session, prepared):
    account = session["account"]
    prepared = dict(prepared, account_username=account["username"])
    with closing(db()) as connection:
        connection.execute(
            "UPDATE admin_e2e_runs SET username=?,status='submitting',cost=?,points_before=?,updated_at=? WHERE run_id=?",
            (account["username"], int(prepared["cost"]), int(account["points"]),
             int(time.time()), run_id),
        )
        connection.commit()
    project_id = str(prepared.get("reuse_project_id") or "")
    if project_id:
        state = _short_drama_preview_fixture_state(account["username"], project_id)
        if not state:
            raise ValueError("短剧预览私有素材快照已失效")
        with closing(db()) as connection:
            connection.execute(
                "UPDATE admin_e2e_runs SET acceptance_id=?,updated_at=? WHERE run_id=?",
                (project_id, int(time.time()), run_id),
            )
            connection.commit()
        _e2e_project_evidence(
            run_id, project_id=project_id, plan_id=state["plan_id"],
            required_shot_keys=state["required_shot_keys"],
            ready_shot_keys=state["ready_shot_keys"],
            missing_shot_keys=state["missing_shot_keys"],
            provider_jobs={}, quote_tokens={}, submitted_job_ids=[],
            fixture_reused=True,
        )
    else:
        state = _prepare_short_drama_preview_project(
            run_id, session["token"], prepared
        )
        if not state:
            raise ValueError("短剧预览私有素材项目建立失败")
    with closing(db()) as connection:
        connection.execute(
            "UPDATE admin_e2e_runs SET status='queued',error='',updated_at=? WHERE run_id=?",
            (int(time.time()), run_id),
        )
        connection.commit()
    _advance_short_drama_preview_e2e(run_id, admin_token)
    return next(run for run in list_e2e_runs(100) if run["run_id"] == run_id)


def _resume_short_drama_preview_runs(admin_token):
    with closing(db()) as connection:
        run_ids = [row[0] for row in connection.execute(
            "SELECT run_id FROM admin_e2e_runs "
            "WHERE operation_id='short_drama.live_action.preview' "
            "AND status IN ('queued','running') ORDER BY created_at"
        ).fetchall()]
    for run_id in run_ids:
        try:
            _advance_short_drama_preview_e2e(run_id, admin_token)
        except Exception:
            pass


def _advance_short_drama_delivery_e2e(run_id, admin_token):
    row, evidence = _preview_e2e_row(run_id)
    if not row or row["status"] not in {"queued", "running", "submitting"}:
        return False
    session = auth_admin_request(
        "/api/auth/admin/e2e/session", admin_token, method="POST", payload={}
    )
    account, token = session["account"], session["token"]
    if row["username"] and row["username"] != account["username"]:
        raise ValueError("正式交付批次与专用测试账号不一致")
    project_id = str(row["acceptance_id"] or evidence.get("project_id") or "")
    if not project_id:
        raise ValueError("正式交付批次缺少私有短剧项目")

    if not evidence.get("refinement_confirmed"):
        workspace = _content_e2e_get(
            "/api/gen/short-drama/refinement?project_id="
            + urllib.parse.quote(project_id), token,
        )
        capability = workspace.get("billing") or {}
        if not (capability.get("deliverable") is True
                and capability.get("mode") == "local_ffmpeg"):
            raise ValueError("短剧 1080p 正式交付执行器未通过运行检查")
        current = workspace.get("current_refinement") or {}
        if current.get("issues"):
            raise ValueError("当前短剧预览仍有待处理问题")
        acceptance = workspace.get("acceptance") or {}
        if current.get("status") != "confirmed" or acceptance.get("valid") is not True:
            current = _short_drama_e2e_request(
                "/api/gen/short-drama/refinement/confirm", token, {
                    "project_id": project_id, "version_id": current.get("id"),
                    "checklist": {key: True for key in (
                        (workspace.get("acceptance_requirements") or {})
                        .get("checklist_keys") or []
                    )},
                    "source_hashes": (
                        workspace.get("acceptance_requirements") or {}
                    ).get("source_hashes") or {},
                }, run_id, "confirm-formal-delivery",
            )
        evidence = _e2e_project_evidence(
            run_id, project_id=project_id, capability_verified=True,
            refinement_confirmed=True,
            refinement_version_id=str(current.get("id") or ""),
        )

    delivery_job_id = str(evidence.get("delivery_job_id") or "")
    if not delivery_job_id:
        quote = _short_drama_e2e_request(
            "/api/gen/short-drama/delivery/quote", token,
            {"project_id": project_id,
             "version_id": evidence["refinement_version_id"]},
            run_id, "quote-formal-delivery",
        )
        if not (quote.get("deliverable") is True
                and quote.get("resolution") == "1080p"
                and int(quote.get("cost") or 0) == 0):
            raise ValueError("正式交付报价未满足 1080p、可交付、0 点合同")
        job = _short_drama_e2e_request(
            "/api/gen/short-drama/delivery/jobs", token,
            {"project_id": project_id, "quote_token": quote.get("quote_token")},
            run_id, "submit-formal-delivery",
        )
        delivery_job_id = str(job.get("id") or "")
        if not delivery_job_id:
            raise E2ESubmitUncertain("正式交付任务响应缺少 delivery_job_id")
        evidence = _e2e_project_evidence(
            run_id, quote_verified=True, delivery_job_id=delivery_job_id,
            delivery_status=str(job.get("status") or "queued"),
        )

    job = _content_e2e_get(
        "/api/gen/short-drama/delivery/jobs/"
        + urllib.parse.quote(delivery_job_id)
        + "?project_id=" + urllib.parse.quote(project_id), token, timeout=300,
    )
    status = str(job.get("status") or "unknown")
    _e2e_project_evidence(
        run_id, delivery_status=status,
        delivery_phase=str(job.get("phase") or ""),
        delivery_error=(job.get("error") or {}).get("detail", ""),
    )
    if status in {"queued", "running"}:
        with closing(db()) as connection:
            connection.execute(
                "UPDATE admin_e2e_runs SET status='running',updated_at=? WHERE run_id=?",
                (int(time.time()), run_id),
            )
            connection.commit()
        return True
    if status != "succeeded":
        raise ValueError((job.get("error") or {}).get("detail") or "正式交付任务失败")

    workspace = _content_e2e_get(
        "/api/gen/short-drama/refinement?project_id="
        + urllib.parse.quote(project_id), token,
    )
    delivery = workspace.get("current_delivery") or {}
    snapshot = delivery.get("snapshot") or {}
    validation = snapshot.get("media_validation") or {}
    probe = validation.get("probe") or {}
    video, audio = probe.get("video") or {}, probe.get("audio")
    media_ok = bool(
        delivery.get("job_id") == delivery_job_id
        and snapshot.get("deliverable") is True
        and snapshot.get("resolution") == "1080p"
        and int(video.get("width") or 0) == 1920
        and int(video.get("height") or 0) == 1080
        and audio
        and abs(int(probe.get("duration_ms") or 0) - 30000) <= 1500
    )
    artifact = _verify_local_artifact({
        "result_file": str(snapshot.get("output_file") or ""),
        "result_url": str(snapshot.get("playback_url") or delivery.get("url") or ""),
        "delivery_verified": False, "artifact_check": "not_recorded",
    })
    attempt = _short_drama_delivery_attempt_state(
        account["username"], project_id, delivery_job_id
    ) or {}
    refreshed = auth_admin_request(
        "/api/auth/admin/e2e/session", admin_token, method="POST", payload={}
    )
    points_after = int(refreshed["account"]["points"])
    billing_ok = bool(
        int(row["points_before"] or 0) == points_after
        and attempt.get("attempt_state") == "linked"
        and int(attempt.get("attempt_cost") or 0) == 0
        and int(attempt.get("job_cost") or 0) == 0
    )
    delivered = bool(media_ok and artifact.get("delivery_verified"))
    evidence = _e2e_project_evidence(
        run_id, delivery_verified=delivered,
        artifact_check=artifact.get("artifact_check"),
        result_file=str(snapshot.get("output_file") or ""),
        result_url=str(snapshot.get("playback_url") or delivery.get("url") or ""),
        media_verified=media_ok, attempt_state=attempt.get("attempt_state"),
        billing_verified=billing_ok,
    )
    if not delivered:
        raise ValueError("1080p 正式成片未通过分辨率、音频、时长或解码验收")
    if not billing_ok:
        raise ValueError("正式交付 0 点任务与测试账号账务不一致")
    with closing(db()) as connection:
        connection.execute(
            """UPDATE admin_e2e_runs SET status='completed',cost=0,points_after=?,
                      evidence_json=?,error='',updated_at=? WHERE run_id=?""",
            (points_after, json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
             int(time.time()), run_id),
        )
        connection.commit()
    return True


def _submit_short_drama_delivery_e2e_run(run_id, admin_token, session, prepared):
    account = session["account"]
    project_id = str(prepared.get("reuse_project_id") or "")
    with closing(db()) as connection:
        connection.execute(
            """UPDATE admin_e2e_runs SET username=?,status='running',cost=0,
                      acceptance_id=?,points_before=?,updated_at=? WHERE run_id=?""",
            (account["username"], project_id, int(account["points"]),
             int(time.time()), run_id),
        )
        connection.commit()
    _e2e_project_evidence(run_id, project_id=project_id)
    _advance_short_drama_delivery_e2e(run_id, admin_token)
    return next(run for run in list_e2e_runs(100) if run["run_id"] == run_id)


def _resume_short_drama_delivery_runs(admin_token):
    with closing(db()) as connection:
        run_ids = [row[0] for row in connection.execute(
            "SELECT run_id FROM admin_e2e_runs "
            "WHERE operation_id='short_drama.live_action.delivery' "
            "AND status IN ('submitting','queued','running') ORDER BY created_at"
        ).fetchall()]
    for run_id in run_ids:
        try:
            _advance_short_drama_delivery_e2e(run_id, admin_token)
        except ValueError as exc:
            with closing(db()) as connection:
                connection.execute(
                    "UPDATE admin_e2e_runs SET status='failed',error=?,updated_at=? WHERE run_id=?",
                    (str(exc)[:300], int(time.time()), run_id),
                )
                connection.commit()
        except Exception:
            pass


def _recover_short_drama_unknown(admin_token):
    with closing(db()) as connection:
        row = connection.execute(
            """SELECT * FROM admin_e2e_runs
               WHERE operation_id='short_drama.live_action.script_planning'
                 AND status='unknown'
               ORDER BY created_at LIMIT 1"""
        ).fetchone()
        if not row:
            return False
        claimed = connection.execute(
            "UPDATE admin_e2e_runs SET status='submitting',updated_at=? WHERE run_id=? AND status='unknown'",
            (int(time.time()), row["run_id"]),
        ).rowcount
        connection.commit()
    if not claimed:
        return False
    run_id = row["run_id"]
    try:
        session = auth_admin_request(
            "/api/auth/admin/e2e/session", admin_token, method="POST", payload={}
        )
        prepared = _e2e_prepare_operation(session, row["operation_id"])
        _submit_short_drama_e2e_run(run_id, admin_token, session, prepared)
        return True
    except Exception as exc:
        uncertain = isinstance(exc, E2ESubmitUncertain)
        with closing(db()) as connection:
            connection.execute(
                "UPDATE admin_e2e_runs SET status=?,error=?,updated_at=? WHERE run_id=?",
                ("unknown" if uncertain else "failed",
                 ("重启后的短剧项目自动恢复失败：" + str(exc))[:300],
                 int(time.time()), run_id),
            )
            connection.commit()
        return False


def _submit_short_drama_e2e_run(run_id, admin_token, session, prepared):
    account = session["account"]
    token = session["token"]
    project_id = ""
    project_revision = None
    evidence = {}
    failure = None
    has_uncertain = False
    with closing(db()) as connection:
        connection.execute(
            """UPDATE admin_e2e_runs SET username=?,status='running',cost=0,
                      points_before=?,points_after=?,updated_at=? WHERE run_id=?""",
            (account["username"], int(account["points"]), int(account["points"]),
             int(time.time()), run_id),
        )
        connection.commit()
    try:
        imported = _short_drama_e2e_request(
            "/api/gen/short-drama/projects/import", token,
            prepared["payload"], run_id, "import",
        )
        project_id = str(imported["id"])
        project_revision = int(imported["revision"])
        evidence["imported"] = bool(project_id)
        with closing(db()) as connection:
            connection.execute(
                "UPDATE admin_e2e_runs SET acceptance_id=?,evidence_json=?,updated_at=? WHERE run_id=?",
                (project_id, json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
                 int(time.time()), run_id),
            )
            connection.commit()

        confirmed = _short_drama_e2e_request(
            "/api/gen/short-drama/conversation/messages", token,
            {"project_id": project_id, "conversation_revision": 1,
             "message": "确认尊重原稿并生成"}, run_id, "confirm-direction",
        )
        conversation = confirmed["conversation"]
        evidence["direction_confirmed"] = bool(
            (conversation.get("understanding") or {}).get("direction_confirmed")
        )
        if not evidence["direction_confirmed"]:
            raise ValueError("短剧原稿方向没有确认")

        generated = _short_drama_e2e_request(
            "/api/gen/short-drama/conversation/script/generate", token,
            {"project_id": project_id,
             "conversation_revision": int(conversation["revision"]),
             "instruction": "尊重原稿"}, run_id, "generate-script",
        )
        script = generated["current_script"]
        evidence["script_version_id"] = str(script["id"])
        evidence["script_generated"] = bool(evidence["script_version_id"])

        locked = _short_drama_e2e_request(
            "/api/gen/short-drama/conversation/script/lock", token,
            {"project_id": project_id,
             "conversation_revision": int(generated["conversation"]["revision"]),
             "version_id": script["id"]}, run_id, "lock-script",
        )
        evidence["script_locked"] = (
            (locked.get("conversation") or {}).get("state") == "script_locked"
        )
        if not evidence["script_locked"]:
            raise ValueError("短剧剧本没有锁定")

        prepared_plan = _short_drama_e2e_request(
            "/api/gen/short-drama/preflight/generate", token,
            {"project_id": project_id,
             "conversation_revision": int(locked["conversation"]["revision"]),
             "quality_route": "quick_draft"}, run_id, "generate-preflight",
        )
        current_plan = prepared_plan["current_plan"]
        plan = current_plan["plan"]
        evidence["plan_id"] = str(current_plan["id"])
        evidence["preflight_ready"] = bool(
            prepared_plan.get("state") == "ready_for_confirmation" and plan.get("ready")
        )
        if not evidence["preflight_ready"]:
            raise ValueError("短剧制作预检没有准备完成")

        accepted = _short_drama_e2e_request(
            "/api/gen/short-drama/preflight/confirm", token,
            {"project_id": project_id, "plan_id": current_plan["id"],
             "plan_version": int(current_plan["version"]),
             "accepted_issue_keys": list(plan.get("required_acceptance") or [])},
            run_id, "confirm-preflight",
        )
        evidence["preflight_confirmed"] = bool(
            accepted.get("state") == "confirmed"
            and (accepted.get("current_plan") or {}).get("status") == "confirmed"
        )
        if not evidence["preflight_confirmed"]:
            raise ValueError("短剧制作计划没有确认")
    except Exception as exc:
        failure = exc
        has_uncertain = isinstance(exc, E2ESubmitUncertain)

    if project_id:
        try:
            abandoned = _short_drama_e2e_request(
                "/api/gen/short-drama/projects/live-action/abandon", token,
                {"project_id": project_id, "revision": project_revision},
                run_id, "abandon-project",
            )
            evidence["project_cleaned"] = bool(abandoned.get("deleted"))
        except Exception as exc:
            evidence["project_cleaned"] = False
            evidence["cleanup_error"] = str(exc)[:180]
            has_uncertain = has_uncertain or isinstance(exc, E2ESubmitUncertain)
            failure = (exc if failure is None else RuntimeError(
                "%s；临时项目清理失败：%s" % (failure, exc)
            ))
    points_after = int(account["points"])
    try:
        refreshed = auth_admin_request(
            "/api/auth/admin/e2e/session", admin_token, method="POST", payload={}
        )
        points_after = int(refreshed["account"]["points"])
        if points_after != int(account["points"]):
            raise ValueError("0 点短剧旅程发生了点数变化，禁止标记通过")
    except Exception as exc:
        evidence["billing_error"] = str(exc)[:180]
        failure = (exc if failure is None else RuntimeError(
            "%s；账务复核失败：%s" % (failure, exc)
        ))
    with closing(db()) as connection:
        connection.execute(
            """UPDATE admin_e2e_runs SET acceptance_id=?,evidence_json=?,
                      points_after=?,updated_at=? WHERE run_id=?""",
            (project_id, json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
             points_after, int(time.time()), run_id),
        )
        connection.commit()
    if has_uncertain:
        failure = (E2ESubmitRejected(
            "短剧步骤响应不确定，但临时项目已确认清理；本次按失败终态记录"
        ) if evidence.get("project_cleaned") else E2ESubmitUncertain(
            "短剧步骤或项目清理响应不确定；保留原批次等待幂等恢复"
        ))
    if failure is not None:
        raise failure
    if not evidence.get("project_cleaned"):
        raise ValueError("短剧质检项目没有完成自动清理")

    with closing(db()) as connection:
        connection.execute(
            """UPDATE admin_e2e_runs SET status='completed',acceptance_id=?,
                      evidence_json=?,points_after=?,error='',updated_at=? WHERE run_id=?""",
            (project_id, json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
             points_after, int(time.time()), run_id),
        )
        connection.commit()
    return next(run for run in list_e2e_runs(100) if run["run_id"] == run_id)


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
        if operation_id == "short_drama.live_action.script_planning":
            return _submit_short_drama_e2e_run(run_id, admin_token, session, prepared)
        if operation_id == "short_drama.live_action.character_reference":
            return _submit_short_drama_character_e2e_run(
                run_id, admin_token, session, prepared
            )
        if operation_id == "short_drama.live_action.shot_video":
            return _submit_short_drama_shot_e2e_run(
                run_id, admin_token, session, prepared
            )
        if operation_id == "short_drama.live_action.preview":
            return _submit_short_drama_preview_e2e_run(
                run_id, admin_token, session, prepared
            )
        if operation_id == "short_drama.live_action.delivery":
            return _submit_short_drama_delivery_e2e_run(
                run_id, admin_token, session, prepared
            )
        payload = prepared["payload"]
        payload["qa_run_id"] = run_id
        endpoint = prepared["endpoint"]
        idem = "e2e:" + run_id
        transaction_key = "job-charge:%s:%s:%s" % (account["username"], endpoint, idem)
        result = (_content_e2e_upload(endpoint, session["token"], payload, idem, cost)
                  if endpoint.startswith("/api/gen/breakdown/local-upload?")
                  else _content_e2e_request(endpoint, session["token"], payload, idem, cost))
        try:
            job_id = int(result["job_id"])
            points_after = int(result["points_left"])
            with closing(db()) as connection:
                connection.execute(
                    """UPDATE admin_e2e_runs SET username=?,status='queued',job_id=?,cost=?,
                              points_before=?,points_after=?,transaction_key=?,updated_at=? WHERE run_id=?""",
                    (account["username"], job_id, cost, int(account["points"]),
                     points_after, transaction_key, int(time.time()), run_id),
                )
                connection.commit()
        except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
            raise E2ESubmitUncertain("任务已提交，但本地受理证据写入不完整") from exc
    except E2ESubmitUncertain as exc:
        with closing(db()) as connection:
            connection.execute(
                "UPDATE admin_e2e_runs SET status='unknown',error=?,updated_at=? WHERE run_id=?",
                ("提交结果未知，禁止自动重试：" + str(exc)[:140], int(time.time()), run_id),
            )
            connection.commit()
        raise RuntimeError("提交结果未知；请先按测试批次核对，禁止再次点击")
    except Exception as exc:
        if (retry_capacity and getattr(exc, "status", 0) == 429
                and (getattr(exc, "body", {}) or {}).get("code") == "active_job_cap"):
            with closing(db()) as connection:
                connection.execute(
                    "UPDATE admin_e2e_runs SET status='planned',error=?,updated_at=? WHERE run_id=?",
                    ("等待测试账号任务位：" + str(exc)[:240], int(time.time()), run_id),
                )
                connection.commit()
            return None
        if operation_id == "short_drama.live_action.character_reference":
            try:
                _cleanup_short_drama_character_draft(run_id, session["token"])
            except Exception as cleanup_exc:
                exc = RuntimeError("%s；临时项目清理失败：%s" % (exc, cleanup_exc))
        elif operation_id == "short_drama.live_action.shot_video":
            try:
                _delete_short_drama_shot_project(run_id, session["token"])
            except Exception as cleanup_exc:
                exc = RuntimeError("%s；临时项目清理失败：%s" % (exc, cleanup_exc))
        elif operation_id == "short_drama.live_action.preview":
            _, evidence = _preview_e2e_row(run_id)
            if evidence.get("submitted_job_ids"):
                with closing(db()) as connection:
                    connection.execute(
                        "UPDATE admin_e2e_runs SET status='running',error=?,updated_at=? WHERE run_id=?",
                        ("预览编排暂时中断，将按原幂等键继续：" + str(exc)[:180],
                         int(time.time()), run_id),
                    )
                    connection.commit()
                return next(run for run in list_e2e_runs(100)
                            if run["run_id"] == run_id)
        elif operation_id == "short_drama.live_action.delivery":
            _, evidence = _preview_e2e_row(run_id)
            if evidence.get("delivery_job_id"):
                with closing(db()) as connection:
                    connection.execute(
                        "UPDATE admin_e2e_runs SET status='running',error=?,updated_at=? WHERE run_id=?",
                        ("正式交付已受理，将继续核对原任务：" + str(exc)[:180],
                         int(time.time()), run_id),
                    )
                    connection.commit()
                return next(run for run in list_e2e_runs(100)
                            if run["run_id"] == run_id)
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
    _recover_short_drama_unknown(admin_token)
    with E2E_RUN_LOCK:
        if any(run["status"] in E2E_ACTIVE_STATUSES for run in list_e2e_runs(100)):
            raise ValueError("已有一条生产链测试或验收批次在运行，请等待终态后再提交")
        run_id = _insert_e2e_run(actor, operation_id)
        return _submit_e2e_run(run_id, admin_token)


def _e2e_run_passed(run):
    stages = run.get("stages") or []
    return bool(
        run.get("status") == "completed"
        and tuple(stage.get("key") for stage in stages) == E2E_STAGE_KEYS
        and all(stage.get("state") == "passed" for stage in stages)
    )


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
            for mode in feature.get("modes") or []
            if all(feature_flags.is_enabled(key)
                   for key in (list(feature.get("flag_keys") or [])
                               + list(mode.get("flag_keys") or [])))]


def e2e_batch_preflight(admin_token, page_key, include_fresh=False):
    if not points_domain:
        raise RuntimeError("点数模块不可用")
    _recover_short_drama_unknown(admin_token)
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
    blocked_items = [item for item in items if not item["ready"]]
    total_cost = sum(item["cost"] for item in ready_items)
    points = int(account.get("points") or 0)
    blocker = ""
    if not account.get("membership_active"):
        blocker = "专用测试账号会员未生效"
    elif page_key in {"audio", "banana", "collect"} and blocked_items:
        page_name = {"audio": "音频", "banana": "图片", "collect": "内容爬取"}[page_key]
        blocker = "%s完整旅程要求客户模式全部准备好，当前仍有 %s 项未准备" % (page_name, len(blocked_items))
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
        "audio_fixture_required": bool(
            page_key == "audio" and any(
                item["operation_id"] == "audio.tts.personal" and not item["ready"]
                for item in items
            )
        ),
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
            _resume_short_drama_character_runs(admin_token)
            _resume_short_drama_shot_runs(admin_token)
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


def _character_reference_operation_stat(since):
    if not JOB_DB.exists():
        return None, None
    try:
        with closing(sqlite3.connect(str(JOB_DB), timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            if not connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='short_drama_character_reference_jobs'").fetchone():
                return None, None
            rows = connection.execute(
                """SELECT job_id,status,error,created_at,updated_at
                   FROM short_drama_character_reference_jobs
                   WHERE created_at>=? ORDER BY created_at DESC""",
                (since,),
            ).fetchall()
    except sqlite3.Error:
        return None, "角色标准图证据读取失败"
    if not rows:
        return None, None
    bucket = {
        "operation": "short_drama.live_action.character_reference",
        "total": 0, "done": 0, "error": 0, "running": 0, "other": 0,
    }
    for row in rows:
        _count_status(bucket, {
            "done": "completed", "failed": "failed",
            "linked": "running", "ready": "running",
        }.get(str(row["status"] or "").lower(), "unknown"))
    latest = _e2e_job_evidence(rows[0]["job_id"])
    if not latest:
        return None, "角色标准图关联的任务证据不存在"
    state = str(rows[0]["status"] or "").lower()
    if state == "failed":
        latest["status"] = "failed"
        latest["error"] = str(rows[0]["error"] or latest.get("error") or "")[:300]
    elif state in {"linked", "ready"} and latest.get("status") in {"done", "completed"}:
        latest["status"] = "processing"
    bucket["latest"] = latest
    return _finish_stats([bucket])[0], None


def _short_drama_shot_operation_stat(since):
    if not JOB_DB.exists():
        return None, None
    try:
        with closing(sqlite3.connect(str(JOB_DB), timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            if not connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='short_drama_provider_shot_jobs'").fetchone():
                return None, None
            rows = connection.execute(
                """SELECT j.*,a.state AS attempt_state
                   FROM short_drama_provider_shot_jobs j
                   LEFT JOIN short_drama_provider_shot_attempts a ON a.job_id=j.id
                   WHERE j.created_at>=? ORDER BY j.created_at DESC""",
                (since,),
            ).fetchall()
    except sqlite3.Error:
        return None, "短剧逐镜证据读取失败"
    if not rows:
        return None, None
    bucket = {
        "operation": "short_drama.live_action.shot_video",
        "total": 0, "done": 0, "error": 0, "running": 0, "other": 0,
    }
    for row in rows:
        _count_status(bucket, {
            "succeeded": "completed", "failed": "failed", "canceled": "failed",
            "billing": "running", "queued": "running", "submitting": "running",
            "running": "running", "submit_unknown": "unknown",
        }.get(str(row["status"] or "").lower(), "unknown"))
    row = rows[0]
    try:
        result = json.loads(row["result_json"] or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        result = {}
    try:
        error = json.loads(row["error_json"] or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        error = {}
    artifact = _verify_local_artifact({
        "result_file": str(result.get("file") or ""),
        "result_url": str(result.get("url") or ""),
        "_artifact_media_type": "video",
    })
    bucket["latest"] = {
        "job_id": None, "project_id": row["project_id"],
        "status": row["status"], "created_at": int(row["created_at"] or 0),
        "updated_at": int(row["updated_at"] or 0), "business_accepted": True,
        "business_id_type": "provider_job_id", "provider_task_id": row["provider_job_id"],
        "provider_accepted": bool(row["provider_job_id"]),
        "route_provider": row["provider"], "completed": row["status"] == "succeeded",
        "result_file": artifact.get("result_file") or None,
        "result_url": artifact.get("result_url") or None,
        "delivery_verified": bool(artifact.get("delivery_verified")),
        "artifact_check": artifact.get("artifact_check") or "not_recorded",
        "cost": int(row["cost"] or 0), "refund_state": 1 if row["attempt_state"] == "refunded" else 0,
        "billing_state": "refunded" if row["attempt_state"] == "refunded" else (
            "charged" if row["attempt_state"] == "done" else "pending"
        ),
        "balance_state": "consistent" if row["attempt_state"] in {"done", "refunded"} else "pending",
        "error": str(error.get("detail") or "")[:300],
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
                              CASE WHEN json_valid(payload) THEN LOWER(COALESCE(json_extract(payload,'$.want[0]'),'comments')) ELSE 'comments' END AS collect_mode,
                              CASE WHEN json_valid(payload) THEN LOWER(COALESCE(json_extract(payload,'$.cine_mode'),'')) ELSE '' END AS cine_mode,
                              CASE WHEN json_valid(payload) THEN CAST(COALESCE(json_extract(payload,'$.line'),'') AS TEXT) ELSE '' END AS line,
                              CASE WHEN json_valid(payload) AND json_type(payload,'$.reference_images')='array'
                                   THEN json_array_length(payload,'$.reference_images')
                                   WHEN json_valid(payload) AND json_type(payload,'$.image')='text'
                                   THEN 1 ELSE 0 END AS reference_count,
                              CASE WHEN json_valid(payload) THEN COALESCE(json_extract(payload,'$.batch_id'),'') ELSE '' END AS batch_id,
                              CASE WHEN json_valid(payload) THEN LOWER(COALESCE(json_extract(payload,'$.operation'),'')) ELSE '' END AS operation,
                              CASE WHEN json_valid(payload) THEN COALESCE(json_extract(payload,'$.upscale'),0) ELSE 0 END AS upscale,
                              CASE WHEN json_valid(payload) AND
                                        (json_type(payload,'$._short_drama_video')='object' OR
                                         json_type(payload,'$.short_drama_binding')='object')
                                   THEN 'short-drama'
                                   WHEN json_valid(payload) THEN LOWER(COALESCE(json_extract(payload,'$.source_page'),''))
                                   ELSE '' END AS source_page,
                              CASE WHEN json_valid(payload) THEN LOWER(COALESCE(json_extract(payload,'$.provider'),'')) ELSE '' END AS provider,
                              CASE WHEN json_valid(payload) THEN LOWER(COALESCE(json_extract(payload,'$.model'),'')) ELSE '' END AS model,
                              CASE WHEN json_valid(payload) THEN LOWER(COALESCE(json_extract(payload,'$.variant'),'')) ELSE '' END AS variant,
                              CASE WHEN json_valid(payload) THEN LOWER(COALESCE(json_extract(payload,'$.voice_scope'),'')) ELSE '' END AS voice_scope,
                              CASE WHEN json_valid(payload) THEN LOWER(COALESCE(json_extract(payload,'$.pipeline'),'')) ELSE '' END AS pipeline,
                              CASE WHEN json_valid(payload) THEN LOWER(COALESCE(json_extract(payload,'$.format'),'')) ELSE '' END AS format,
                              CASE WHEN json_valid(payload) THEN COALESCE(json_extract(payload,'$.style'),'') ELSE '' END AS style,
                              CASE WHEN json_valid(payload) THEN LOWER(COALESCE(json_extract(payload,'$.source_type'),'')) ELSE '' END AS source_type,
                              CASE WHEN json_valid(payload) AND json_type(payload,'$.mask')='text' THEN 1 ELSE 0 END AS mask_present,
                              CASE WHEN json_valid(%s) THEN COALESCE(json_extract(%s,'$.video_url'),json_extract(%s,'$.image_url'),json_extract(%s,'$.url'),json_extract(%s,'$.urls[0]'),'') ELSE '' END AS result_url,
                              CASE WHEN json_valid(%s) THEN COALESCE(json_extract(%s,'$.video_file'),json_extract(%s,'$.image_file'),json_extract(%s,'$.file'),json_extract(%s,'$.files[0]'),'') ELSE '' END AS result_file,
                              CASE WHEN json_valid(%s) THEN COALESCE(json_extract(%s,'$.provider_task_id'),json_extract(%s,'$.request_id'),json_extract(%s,'$.provider_video_id'),json_extract(%s,'$.video_id'),json_extract(%s,'$.provider_avatar_id'),'') ELSE '' END AS provider_result_id,
                              %s AS result_json
                       FROM jobs WHERE created_at>=? ORDER BY created_at DESC""" % (
                           refunded_sql, error_sql,
                           result_sql, result_sql, result_sql, result_sql, result_sql,
                           result_sql, result_sql, result_sql, result_sql, result_sql,
                           result_sql, result_sql, result_sql, result_sql, result_sql, result_sql,
                           result_sql,
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
            "collect_mode": row["collect_mode"],
            "cine_mode": row["cine_mode"], "line": row["line"],
            "reference_count": row["reference_count"], "batch_id": row["batch_id"],
            "operation": row["operation"], "upscale": row["upscale"],
            "source_page": row["source_page"], "provider": row["provider"],
            "model": row["model"], "variant": row["variant"],
            "voice_scope": row["voice_scope"],
            "pipeline": row["pipeline"], "format": row["format"],
            "style": row["style"], "source_type": row["source_type"],
            "mask_present": bool(row["mask_present"]),
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
            for key in ("channel", "mode", "cine_mode", "line", "provider", "model", "variant", "voice_scope"):
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
    audio_assets, audio_asset_error = _audio_asset_evidence([
        int(row["id"]) for row in latest_rows.values()
        if str(row["kind"] or "").lower() == "audio"
    ])
    if audio_asset_error:
        evidence_errors.append(audio_asset_error)
    for operation, row in latest_rows.items():
        asset = (
            audio_assets.get(int(row["id"]))
            if str(row["kind"] or "").lower() == "audio"
            else assets.get(int(row["id"]))
        )
        by_operation[operation]["latest"] = _job_evidence(
            row, asset
        )
    compose, compose_error = _compose_operation_stat(since)
    if compose_error:
        evidence_errors.append(compose_error)
    if compose:
        by_operation[compose["operation"]] = compose
    character_reference, character_reference_error = _character_reference_operation_stat(since)
    if character_reference_error:
        evidence_errors.append(character_reference_error)
    if character_reference:
        by_operation[character_reference["operation"]] = character_reference
    short_drama_shot, short_drama_shot_error = _short_drama_shot_operation_stat(since)
    if short_drama_shot_error:
        evidence_errors.append(short_drama_shot_error)
    if short_drama_shot:
        by_operation[short_drama_shot["operation"]] = short_drama_shot
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


_PAYLOAD_FIELD_RE = re.compile(r'"(model|provider|channel|mode|keyword|url|line|variant|source_page|voice_scope)"\s*:\s*"([^"]*)"')


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
                           THEN json_array_length(payload,'$.reference_images')
                           WHEN json_valid(payload) AND json_type(payload,'$.image')='text'
                           THEN 1 ELSE 0 END AS reference_count,
                      CASE WHEN json_valid(payload) THEN COALESCE(json_extract(payload,'$.batch_id'),'') ELSE '' END AS batch_id,
                      CASE WHEN json_valid(payload) THEN LOWER(COALESCE(json_extract(payload,'$.operation'),'')) ELSE '' END AS operation,
                      CASE WHEN json_valid(payload) AND
                                (json_type(payload,'$._short_drama_video')='object' OR
                                 json_type(payload,'$.short_drama_binding')='object')
                           THEN 'short-drama'
                           WHEN json_valid(payload) THEN LOWER(COALESCE(json_extract(payload,'$.source_page'),''))
                           ELSE '' END AS source_page,
                      CASE WHEN json_valid(payload) THEN LOWER(COALESCE(json_extract(payload,'$.provider'),'')) ELSE '' END AS provider,
                      CASE WHEN json_valid(payload) THEN LOWER(COALESCE(json_extract(payload,'$.model'),'')) ELSE '' END AS model,
                      CASE WHEN json_valid(payload) THEN LOWER(COALESCE(json_extract(payload,'$.variant'),'')) ELSE '' END AS variant,
                      CASE WHEN json_valid(payload) THEN LOWER(COALESCE(json_extract(payload,'$.voice_scope'),'')) ELSE '' END AS voice_scope,
                      CASE WHEN json_valid(payload) AND json_type(payload,'$.mask')='text' THEN 1 ELSE 0 END AS mask_present
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
            "provider": row["provider"], "model": row["model"],
            "variant": row["variant"], "voice_scope": row["voice_scope"],
            "mask_present": bool(row["mask_present"]),
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
            _recover_short_drama_unknown(self._token())
            _resume_short_drama_character_runs(self._token())
            _resume_short_drama_shot_runs(self._token())
            _resume_short_drama_preview_runs(self._token())
            _resume_short_drama_delivery_runs(self._token())
            recent_e2e_runs = list_e2e_runs(100)
            e2e_runs = list_latest_e2e_runs()
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
                    "e2e_batches": list_e2e_batches(recent_e2e_runs),
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
        if path == "/api/admin/e2e/browser/run":
            try:
                body = self._body()
                if set(body) != {"operation_id", "confirmation"}:
                    raise ValueError("请求字段必须是 operation_id 和 confirmation")
                if body.get("confirmation") != "RUN_BROWSER":
                    raise ValueError("请明确确认本次客户页真实扣点测试")
                run = start_browser_e2e_run(
                    user.get("username") or "admin", self._token(),
                    str(body.get("operation_id") or "").strip(),
                )
                return self._send(200, {"ok": True, "run": run})
            except ValueError as exc:
                return self._send(400, {"detail": str(exc)})
            except Exception as exc:
                return self._send(getattr(exc, "status", 500), {"detail": str(exc)[:220]})
        if path == "/api/admin/e2e/audio-fixture/prepare":
            try:
                body = self._body()
                if set(body) != {"confirmation"} or body.get("confirmation") != "PREPARE":
                    raise ValueError("请明确确认准备专用测试账号的私有音色")
                result = prepare_audio_e2e_personal_fixture(self._token())
                return self._send(200, {"ok": True, "fixture": result})
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
