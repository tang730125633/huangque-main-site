"""Scoped device authorization and fixed action plans for the Huangque CLI."""

import base64
import hashlib
import hmac
import http.client
import json
import math
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_HALF_UP

try:
    from . import director_workflow_contract
except ImportError:  # Production can launch this module from the server directory.
    import director_workflow_contract


PUBLIC_ORIGIN = os.environ.get("HQ_CLI_PUBLIC_ORIGIN", "https://huangquechuanmei.com").strip().rstrip("/")
DEVICE_TTL = 10 * 60
TOKEN_TTL = 8 * 60 * 60
POLL_INTERVAL = 3
BRIDGE_TOKEN_TTL = 60
QUOTE_TTL = 5 * 60
ACTION_REQUEST_TTL = 30 * 24 * 60 * 60
ACTION_INFLIGHT_TTL = 10 * 60
CLI_CHAT_REQUESTS_PER_MINUTE = 6
CONTENT_BASE = "http://127.0.0.1:8096"
LEADGEN_BASE = "http://127.0.0.1:8100"
IMGGEN_BASE = "http://127.0.0.1:8101"
HERMES_BASE = "http://127.0.0.1:3102"
ADMIN_BASE = "http://127.0.0.1:8098"
CREATOR_AGENT_BASE = os.environ.get("HQ_CREATOR_AGENT_BASE", "http://127.0.0.1:8114").rstrip("/")

SCOPES = {
    "profile:read": "读取账号公开资料与点数",
    "ip12:read": "读取本人 IP12 项目与报告",
    "ip12:write": "创建本人 IP12 项目",
    "ip12:chat": "向本人 IP12 项目提交回答并调用 AI 教练",
    "prompt:optimize": "把提示词发送给黄雀 AI 优化",
    "canvas:read": "读取本人可访问的画布",
    "canvas:write": "创建本人画布",
    "canvas:agent": "把画布快照发送给 AI 生成可确认的操作方案",
    "canvas:edit": "经确认后编辑本人有编辑权限的画布",
    "assets:upload": "上传本人生成所需的临时参考图",
    "tasks:read": "读取本人任务状态与点数流水",
    "assets:read": "读取本人资产与音色",
    "assets:write": "收藏资产并管理本人资产标签",
    "inspiration:read": "读取灵感案例与本人收藏状态",
    "inspiration:write": "经确认后收藏或取消收藏灵感案例",
    "leads:read": "读取本人线索跟进记录",
    "leads:write": "经确认后更新本人线索跟进记录",
    "short-drama:read": "读取本人短剧项目与生产准备状态",
    "short-drama:write": "经确认后创建或删除本人短剧项目",
    "generation:quote": "查询生成、采集或获客任务所需点数",
    "generation:submit": "经二次确认后提交付费任务并扣点",
    "video-compose:read": "读取本人一键成片项目",
    "video-compose:write": "经确认后创建、分析、审核或渲染本人一键成片项目",
    "digital-presenter:read": "读取本人画布中的数字人口播项目",
    "digital-presenter:write": "经确认后创建或更新本人画布中的数字人口播项目",
    "account:write": "经确认后更新本人账号资料、好友、邀请和充值订单",
    "account:read": "读取本人账号通知、邀请、充值与点数记录",
    "creator-agent:read": "读取本人创作助手项目和批次状态",
    "creator-agent:write": "经确认后向本人创作助手提交消息或修改项目",
    "tasks:write": "经确认后删除本人失败的生成任务",
}
SCOPES.update(director_workflow_contract.SCOPE_CONTRACT)
DEFAULT_SCOPES = tuple(SCOPES)
CHANNEL_CATALOG = (
    {"id": "xai", "provider": "xAI API", "category": "视频生成", "features": ["果肉视频生成"],
     "access": "direct", "capabilities": ["video-generate"], "selector": {"channel": "grok"}},
    {"id": "openai", "provider": "OpenAI API", "category": "图片 / 视频", "features": ["黄雀引擎 2", "Sora 2"],
     "access": "mixed", "capabilities": ["image-generate", "video-generate"], "selector": {},
     "selectors": [{"capability": "image-generate", "input": {"provider": "openai"}},
                   {"capability": "video-generate", "input": {"channel": "sora"}}]},
    {"id": "gemini", "provider": "Google Gemini API", "category": "图片 / 视频", "features": ["纳米香蕉", "Omni 视频"],
     "access": "mixed", "capabilities": ["image-generate", "video-generate"], "selector": {},
     "selectors": [{"capability": "image-generate", "input": {"provider": "banana"}},
                   {"capability": "video-generate", "input": {"channel": "omni"}}]},
    {"id": "seedance", "provider": "火山方舟 API", "category": "图片 / 视频", "features": ["Seedream", "Seedance 视频"],
     "access": "direct", "capabilities": ["image-generate", "video-generate"], "selector": {"provider": "seedream", "channel": "micro"}},
    {"id": "minimax", "provider": "MetaSo MiniMax API", "category": "视频生成", "features": ["麦克视频"],
     "access": "direct", "capabilities": ["video-generate"], "selector": {"channel": "minimax"}},
    {"id": "zelong", "provider": "小乐 AI API", "category": "图片生成", "features": ["黄雀引擎 2 备用线路"],
     "access": "routed", "capabilities": ["image-generate"], "selector": {"provider": "xiaole"}},
    {"id": "zelong2", "provider": "泽龙 API", "category": "图片生成", "features": ["泽龙 2 备用线路（维护中）"],
     "access": "registered", "capabilities": [], "selector": {}},
    {"id": "heygen", "provider": "HeyGen API", "category": "数字化 IP / 视频", "features": ["电影化身", "数字人口播", "数字人形象"],
     "access": "managed", "capabilities": [
         "digital-presenter-capability", "digital-presenter-create",
         "digital-ip-text-generate", "digital-ip-batch-generate", "digital-ip-audio-generate",
         "cinematic-open-generate", "cinematic-motion-generate",
     ], "selector": {}},
    {"id": "heygen_relay", "provider": "HeyGen 中转 API", "category": "数字化 IP / 视频", "features": ["中转与下载兜底"],
     "access": "routed", "capabilities": ["tasks", "assets"], "selector": {}},
    {"id": "xiaolevideo", "provider": "小乐视频 API", "category": "图片 / 视频", "features": ["果肉生图", "历史兼容线路"],
     "access": "routed", "capabilities": ["image-generate", "video-generate"], "selector": {}},
    {"id": "runninghub", "provider": "RunningHub API", "category": "视频处理", "features": ["换装换背景 · 线路一"],
     "access": "managed", "capabilities": ["tryon-classic-generate"], "selector": {}},
    {"id": "wavespeed", "provider": "WaveSpeed API", "category": "视频处理", "features": ["换装换背景 · 线路二", "Seedance AI 超清"],
     "access": "managed", "capabilities": ["tryon-fast-generate"], "selector": {}},
    {"id": "cosyvoice", "provider": "阿里百炼 API", "category": "音频生成", "features": ["公共音色", "声音克隆"],
     "access": "direct", "capabilities": ["voices", "audio-generate"], "selector": {}},
    {"id": "tikhub", "provider": "TikHub API", "category": "内容采集 / 获客", "features": ["抖音 / 小红书 / 视频号", "评论与线索"],
     "access": "mixed", "capabilities": [
         "collect", "collect-content", "collect-video", "collect-transcript", "collect-search",
         "leads", "leads-generate",
     ], "selector": {}},
    {"id": "zhipu", "provider": "智谱视觉 API", "category": "内容分析", "features": ["链接分镜拆解"],
     "access": "managed", "capabilities": [], "selector": {}},
    {"id": "cos", "provider": "腾讯云 COS", "category": "基础设施", "features": ["生成结果存储", "参考素材与成片存储"],
     "access": "managed", "capabilities": ["image-upload", "assets"], "selector": {}},
)
CONFIRMATION_ACTIONS = frozenset({
    "ip12-create", "ip12-message", "ip12-delete", "prompt-optimize", "canvas-create", "canvas-ops",
    "canvas-delete", "asset-favorite", "asset-tags", "asset-delete", "video-compose-create", "video-compose-analyze",
    "video-compose-review", "video-compose-render", "video-compose-delete", "digital-presenter-create",
    "digital-presenter-update", "digital-presenter-delete", "voice-clone-create",
    "inspiration-like", "leads-crm-upsert", "leads-delete",
    "short-drama-create", "short-drama-delete",
    "digital-ip-create", "digital-ip-update", "digital-ip-delete",
    "text-video-avatar-import", "text-video-plan",
    "digital-human-oneclick-consent", "digital-human-oneclick-recover",
    "digital-human-oneclick-abandon",
    "director-chat", "director-produce", "director-workflow-create",
    "director-storyboard-update", "director-production-plan",
    "director-production-recover", "director-remake-plan", "director-remake-recover",
    "short-drama-advisor", "short-drama-character-reference-confirm",
    "short-drama-preflight-plan", "short-drama-preflight-confirm",
    "short-drama-autodraft-preflight", "short-drama-autodraft-start",
    "short-drama-delivery-start", "short-drama-completion-confirm",
})

# This is the public contract shared by the CLI, the first-party HTTP bridge,
# and IP12.  `action_plan` remains the only validator and route builder; the
# catalog deliberately describes inputs without exposing private upstream URLs
# or provider credentials.
_ACTION_INPUTS = {
    "account": (), "channels": (), "pricing": (), "director-capability": (),
    "digital-human-oneclick-capability": (),
    "digital-human-oneclick-plan": (
        "script", "narration_mode", "audio_upload_id", "allow_ai_materials",
        "customer_upload_ids",
    ),
    "digital-human-oneclick-consent": (
        "confirmed", "consent_version", "purpose", "run_id", "plan_digest",
        "script", "photo_sha256", "voice_mode", "voice_ref", "voice_sha256",
        "narration_mode", "audio_upload_id", "allow_ai_materials",
        "customer_upload_ids",
    ),
    "digital-human-oneclick-start": (
        "request_id", "consent_token", "plan_digest", "script",
        "narration_mode", "audio_upload_id", "allow_ai_materials",
        "customer_upload_ids", "portrait_upload_id", "voice_key",
    ),
    "digital-human-oneclick-status": ("run_id",),
    "digital-human-oneclick-recover": ("run_id", "request_id"),
    "digital-human-oneclick-abandon": ("run_id", "request_id"),
    "digital-human-oneclick-history": ("limit", "offset"),
    "director-script-generate": ("prompt", "style", "duration", "platform"),
    "director-breakdown": ("url", "urls", "mode"),
    "director-scene-image-generate": ("scenes", "ratio", "quality"),
    "director-scene-video-generate": ("scenes", "channel", "ratio", "duration", "seconds", "resolution", "model", "generate_audio", "reference_upload_ids"),
    "director-scene-talking-generate": ("text", "template", "mode", "style", "voice", "speech_rate", "talking_material"),
    "director-workflows": ("limit", "offset"),
    "director-workflow-create": ("title", "source_job_id", "storyboard", "request_id"),
    "director-workflow": ("workflow_id",),
    "director-storyboard-update": ("workflow_id", "revision", "storyboard"),
    "director-storyboard-export": ("workflow_id",),
    "director-production-plan": ("workflow_id", "output_kind", "options"),
    "director-production-start": ("workflow_id", "plan_digest", "request_id"),
    "director-production-status": ("workflow_id",),
    "director-production-recover": ("workflow_id", "plan_digest", "request_id"),
    "director-remake-plan": ("workflow_id", "mode", "instruction", "options"),
    "director-remake-start": ("workflow_id", "plan_digest", "request_id"),
    "director-remake-status": ("workflow_id",),
    "director-remake-recover": ("workflow_id", "plan_digest", "request_id"),
    "director-chat": ("prompt", "session_id", "page_revision", "page_context", "history", "source_page", "request_id"),
    "director-produce": ("offer_id", "input", "expected_cost", "plan_digest", "quote_token"),
    "text-video-capability": (), "text-video-templates": (),
    "text-video-styles": (), "text-video-voices": (),
    "text-video-generate": ("text", "template", "mode", "style", "voice", "speech_rate", "talking_material"),
    "matrix-template-capability": (), "matrix-template-templates": (),
    "matrix-template-generate": ("top_text", "bottom_text", "template_id", "font_family"),
    "matrix-template-batch-generate": ("top_text", "bottom_text", "template_id", "font_family", "count"),
    "text-video-avatar-import": ("image_upload_id",),
    "text-video-plan": ("text", "template", "mode", "style", "voice", "speech_rate", "ratio"),
    "inspiration-catalog": (), "inspiration-likes": (),
    "inspiration-like": ("id", "favorite"),
    "leads-crm": ("lead_ids",), "leads-crm-upsert": ("lead_id", "intent", "follow_status", "follow_note"),
    "collect-content": ("url",), "collect-video": ("url",), "collect-transcript": ("url",),
    "collect-search": ("platform", "keyword", "page"), "leads-generate": ("url", "platform", "pages", "channels_targets"),
    "video-avatars": ("limit",), "audio-slots": (),
    "video-avatar-create": ("image_data", "name"),
    "voice-clone-create": ("slot_id", "name", "audio_upload_id"),
    "voice-clone-status": ("slot_id",),
    "short-drama-projects": ("page", "page_size"),
    "short-drama-project": ("project_id",), "short-drama-conversation": ("project_id",),
    "short-drama-preflight": ("project_id",),
    "short-drama-advisor": ("messages", "understanding", "expected_field", "field_states", "recommendation_context", "user_message", "request_id"),
    "short-drama-character-reference-generate": ("project_id", "revision", "character_key"),
    "short-drama-character-reference-confirm": ("project_id", "revision", "character_key", "reference_version"),
    "short-drama-preflight-plan": ("project_id", "conversation_revision", "quality_route", "request_id"),
    "short-drama-preflight-confirm": ("project_id", "plan_id", "plan_version", "accepted_issue_keys", "request_id"),
    "short-drama-autodraft-preflight": ("project_id", "plan_id", "shot_key", "character_key", "avatar_id", "execution"),
    "short-drama-autodraft-quote": ("project_id", "plan_id", "shot_key", "character_key", "avatar_id"),
    "short-drama-autodraft-start": ("project_id", "quote_token", "request_id"),
    "short-drama-autodraft-status": ("project_id", "job_id"),
    "short-drama-delivery-quote": ("project_id", "version_id"),
    "short-drama-delivery-start": ("project_id", "quote_token", "request_id"),
    "short-drama-delivery-status": ("project_id", "job_id"),
    "short-drama-completion-readiness": ("project_id",),
    "short-drama-completion": ("project_id",),
    "short-drama-completion-confirm": ("project_id", "revision", "final_version_id", "asset_id", "delivery_hash", "acknowledged", "request_id"),
    "digital-ip-projects": (), "digital-ip-project": ("project_id",), "digital-ip-report": ("project_id",),
    "ip12-projects": (), "ip12-project": ("project_id",), "ip12-report": ("project_id",),
    "ip12-create": ("title",), "ip12-message": ("project_id", "message", "request_id"),
    "ip12-delete": ("project_id",),
    "prompt-optimize": ("prompt", "kind"),
    "canvas-list": ("limit", "offset"), "canvas-get": ("board_id",),
    "canvas-create": ("name", "prompt"), "canvas-agent-plan": ("prompt", "project_id", "snapshot_digest", "scope", "nodes", "edges", "selected_node_ids", "history"),
    "canvas-ops": ("board_id", "expected_version", "op_id", "ops"),
    "tasks": ("days", "kind", "page", "page_size"), "task": ("job_id",),
    "assets": ("kind", "limit", "offset"), "voices": (),
    "asset-favorite": ("kind", "key", "favorite"), "asset-tags": ("kind", "key", "tags"),
    "asset-delete": ("kind", "id", "keys"),
    "canvas-delete": ("board_id",),
    "video-compose-delete": ("project_id", "expected_revision"),
    "digital-presenter-delete": ("board_id", "project_id", "revision"),
    "short-drama-create": ("title", "synopsis", "ratio", "target_duration", "shot_count", "genre", "visual_style", "request_id"),
    "short-drama-delete": ("project_id", "revision"),
    "leads-delete": ("lead_ids",),
    "digital-ip-create": ("title",),
    "digital-ip-update": ("project_id", "revision", "title"),
    "digital-ip-delete": ("project_id", "revision"),
    "video-compose-projects": (), "video-compose-project": ("project_id",),
    "video-compose-create": ("source_asset_id",),
    "video-compose-analyze": ("project_id", "expected_revision"),
    "video-compose-review": ("project_id", "expected_revision", "decisions"),
    "video-compose-render": ("project_id", "expected_revision"),
    "digital-presenter-capability": (), "digital-presenter-project": ("board_id", "project_id"),
    "digital-presenter-create": ("board_id", "request_id", "title", "script_text", "ratio", "resolution", "voice_key", "target_duration"),
    "digital-presenter-update": ("board_id", "project_id", "revision", "title", "script_text", "ratio", "resolution", "voice_key", "target_duration"),
    "image-generate": ("prompt", "provider", "ratio", "quality", "count", "variant", "model", "image_upload_id", "mask_upload_id", "reference_upload_ids"),
    "video-generate": ("prompt", "channel", "ratio", "duration", "seconds", "resolution", "model", "generate_audio", "reference_upload_ids"),
    "video-lipsync": ("video_asset_id", "audio_asset_id", "quality", "dynamic_duration"),
    "audio-generate": ("text", "voice", "speed", "pitch", "volume"),
    "digital-ip-text-generate": ("avatar_id", "image_upload_id", "text", "voice", "ratio", "motion", "subtitle", "subtitle_style", "subtitle_position"),
    "digital-ip-batch-generate": ("avatars", "text", "voice", "ratio", "motion", "subtitle", "subtitle_style", "subtitle_position"),
    "digital-ip-audio-generate": ("avatar_id", "image_upload_id", "audio_file", "audio_upload_id", "ratio", "motion", "subtitle", "subtitle_style", "subtitle_position"),
    "cinematic-open-generate": ("avatar_id", "avatar_ids", "prompt", "ratio", "duration", "enhance_prompt", "reference_image_upload_ids", "reference_video_upload_ids"),
    "cinematic-motion-generate": ("avatar_id", "reference_video_upload_ids", "ratio"),
    "tryon-fast-generate": ("person_image_upload_id", "clothes_upload_id", "seconds"),
    "tryon-classic-generate": ("person_video_upload_id", "clothes_upload_id", "background_upload_id", "seconds"),
}

# Fixed mirrors of normal-user workbench calls that were not in the v0.14
# catalog.  This is deliberately data, not an URL proxy: every entry fixes a
# route, method, owner scope and the exact accepted input keys.
_WEB = "public"
_CONTENT = "content"
_CREATOR = "creator"
WEB_PARITY_ACTIONS = {
    # Creator Agent
    "creator-agent-capability": (_CREATOR, "GET", "/capability", (), (), False, False),
    "creator-agent-bootstrap": (_CREATOR, "GET", "/bootstrap", (), (), False, False),
    "creator-agent-message": (_CREATOR, "POST", "/messages", ("message", "request_id", "project_id", "intent", "payload"), ("message", "request_id", "project_id"), True, True),
    "creator-agent-project-select": (_CREATOR, "POST", "/projects/{project_id}/select", ("project_id",), ("project_id",), True, False),
    "creator-agent-project-rename": (_CREATOR, "POST", "/projects/{project_id}/rename", ("project_id", "title"), ("project_id", "title"), True, False),
    "creator-agent-batch-quote": (_CREATOR, "POST", "/batches/{batch_id}/quote", ("batch_id", "expected_revision"), ("batch_id", "expected_revision"), False, False),
    "creator-agent-batch-confirm": (_CREATOR, "POST", "/batches/{batch_id}/confirm", ("batch_id", "confirmation_id", "expected_revision", "expected_quote_expires_at"), ("batch_id", "confirmation_id", "expected_revision"), True, False),
    "creator-agent-batch-refresh": (_CREATOR, "POST", "/batches/{batch_id}/refresh", ("batch_id",), ("batch_id",), False, False),
    "task-delete": (_CONTENT, "POST", "/api/gen/job/delete", ("job_id",), ("job_id",), True, False),
    # Account, invitation, notification and payment (payment itself stays browser-only).
    "invite-config": (_WEB, "GET", "/api/invite/config", (), (), False, False),
    "invite-dashboard": (_WEB, "GET", "/api/invite/dashboard", (), (), False, False),
    "invite-users": (_WEB, "GET", "/api/invite/users?{query}", ("level", "limit", "offset"), (), False, False),
    "invite-rewards": (_WEB, "GET", "/api/invite/reward-points?{query}", ("limit", "offset"), (), False, False),
    "invite-code": (_WEB, "GET", "/api/invite/code", (), (), False, False),
    "invite-referrer": (_WEB, "GET", "/api/invite/referrer", (), (), False, False),
    "notifications": (_WEB, "GET", "/api/auth/notifications?{query}", ("limit",), (), False, False),
    "notification-read": (_WEB, "POST", "/api/auth/notifications/{notification_id}/read", ("notification_id",), ("notification_id",), True, False),
    "notifications-read-all": (_WEB, "POST", "/api/auth/notifications/read-all", (), (), True, False),
    "profile-update": (_WEB, "POST", "/api/auth/profile", ("display_name",), ("display_name",), True, False),
    "friends": (_WEB, "GET", "/api/auth/friends", (), (), False, False),
    "friend-requests": (_WEB, "GET", "/api/auth/friend-requests", (), (), False, False),
    "friend-request": (_WEB, "POST", "/api/auth/friends/request", ("account_id",), ("account_id",), True, False),
    "friend-request-respond": (_WEB, "POST", "/api/auth/friend-requests/respond", ("request_id", "action"), ("request_id", "action"), True, False),
    "friend-delete": (_WEB, "DELETE", "/api/auth/friends/{username}", ("username",), ("username",), True, False),
    "points-transfer-recipient": (_WEB, "GET", "/api/auth/points/transfer/recipient?{query}", ("account_id",), ("account_id",), False, False),
    "points-transfers": (_WEB, "GET", "/api/auth/points/transfers?{query}", ("limit", "offset"), (), False, False),
    "recharge-packages": (_WEB, "GET", "/api/auth/recharge/packages", (), (), False, False),
    "recharge-orders": (_WEB, "GET", "/api/auth/recharge/orders?{query}", ("limit", "offset"), (), False, False),
    # Asset library and collaborative canvas.
    "asset-marks": (_CONTENT, "GET", "/api/gen/asset/marks?{query}", ("kind",), ("kind",), False, False),
    "asset-batch-delete": (_CONTENT, "POST", "/api/gen/asset/batch-delete", ("assets",), ("assets",), True, False),
    "avatar-rename": (_CONTENT, "POST", "/api/gen/video/avatar-name", ("id", "name"), ("id", "name"), True, False),
    "avatar-delete": (_CONTENT, "POST", "/api/gen/video/avatar-delete", ("id",), ("id",), True, False),
    "voice-rename": (_CONTENT, "POST", "/api/gen/audio/voice-name", ("slot_id", "name"), ("slot_id", "name"), True, False),
    "canvas-members": (_WEB, "GET", "/api/auth/canvas/boards/{board_id}", ("board_id",), ("board_id",), False, False),
    "canvas-member-add": (_WEB, "POST", "/api/auth/canvas/boards/{board_id}/members", ("board_id", "account_id", "role"), ("board_id", "account_id", "role"), True, False),
    "canvas-member-remove": (_WEB, "DELETE", "/api/auth/canvas/boards/{board_id}/members/{username}", ("board_id", "username"), ("board_id", "username"), True, False),
    # Digital IP / IP12 report workflow.
    "digital-ip-diagnose": (_CONTENT, "POST", "/api/gen/digital-ip/diagnose", ("project_id", "revision", "state"), ("project_id", "revision", "state"), True, False),
    "digital-ip-guide": (_CONTENT, "POST", "/api/gen/digital-ip/guide", ("project_id", "revision", "message", "history"), ("project_id", "revision", "message"), True, False),
    "digital-ip-report-generate": (_CONTENT, "POST", "/api/gen/digital-ip/projects/{project_id}/report", ("project_id", "revision", "consent"), ("project_id", "revision", "consent"), True, False),
    "digital-ip-report-confirm": (_CONTENT, "POST", "/api/gen/digital-ip/projects/{project_id}/report-confirm", ("project_id", "revision", "report_id"), ("project_id", "revision", "report_id"), True, False),
    "digital-human-oneclick-heygen-preflight": (_CONTENT, "POST", "/api/gen/digital-human-oneclick/heygen-preflight", (), (), False, False),
    "digital-human-oneclick-material-resolve": (_CONTENT, "POST", "/api/gen/digital-human-v2/material-resolve", ("digital_human_pipeline", "digital_human_stage", "digital_human_run_id", "digital_human_plan_digest", "digital_human_consent_token", "digital_human_script", "digital_human_narration_mode", "digital_human_audio_upload_id", "digital_human_allow_ai_materials", "digital_human_customer_upload_ids", "digital_human_item_index"), ("digital_human_pipeline", "digital_human_stage", "digital_human_run_id", "digital_human_plan_digest", "digital_human_consent_token", "digital_human_narration_mode", "digital_human_allow_ai_materials", "digital_human_customer_upload_ids", "digital_human_item_index"), True, False),
    # Short-drama center, workspace, graph, draft, refinement and delivery.
    "short-drama-project-update": (_CONTENT, "PUT", "/api/gen/short-drama/project?id={project_id}", ("project_id", "revision", "title", "synopsis", "genre", "ratio", "target_duration", "shot_count", "visual_style", "target_platform", "point_budget", "characters", "character_contract", "script", "shots"), ("project_id", "revision"), True, False),
    "short-drama-project-promote": (_CONTENT, "POST", "/api/gen/short-drama/projects/promote", ("project", "planning_messages", "confirmed_contract", "request_id"), ("project", "planning_messages", "confirmed_contract", "request_id"), True, True),
    "short-drama-project-import": (_CONTENT, "POST", "/api/gen/short-drama/projects/import", ("title", "synopsis", "ratio", "target_duration", "shot_count", "visual_style", "source_text", "filename", "import_mode", "content_type", "character_contract", "genre", "source_requirement", "request_id"), ("title", "synopsis", "ratio", "target_duration", "shot_count", "visual_style", "source_text", "filename", "import_mode", "request_id"), True, True),
    "short-drama-core-story": (_CONTENT, "POST", "/api/gen/short-drama/projects/live-action/core-story", ("project_id", "revision", "core_story"), ("project_id", "revision", "core_story"), True, False),
    "short-drama-project-finalize": (_CONTENT, "POST", "/api/gen/short-drama/projects/live-action/finalize", ("project_id", "revision"), ("project_id", "revision"), True, False),
    "short-drama-project-abandon": (_CONTENT, "POST", "/api/gen/short-drama/projects/live-action/abandon", ("project_id", "revision", "request_id"), ("project_id", "revision", "request_id"), True, True),
    "short-drama-script-message": (_CONTENT, "POST", "/api/gen/short-drama/conversation/messages", ("project_id", "revision", "message", "request_id"), ("project_id", "revision", "message", "request_id"), True, True),
    "short-drama-script-generate": (_CONTENT, "POST", "/api/gen/short-drama/conversation/script/generate", ("project_id", "revision", "instruction", "confirmed_contract", "request_id"), ("project_id", "revision", "request_id"), True, True),
    "short-drama-shot-update": (_CONTENT, "POST", "/api/gen/short-drama/conversation/script/shot/update", ("project_id", "revision", "version_id", "shot_key", "changes", "instruction", "request_id"), ("project_id", "revision", "version_id", "shot_key", "changes", "request_id"), True, True),
    "short-drama-shot-regenerate": (_CONTENT, "POST", "/api/gen/short-drama/conversation/script/shot/regenerate", ("project_id", "revision", "version_id", "shot_key", "instruction", "request_id"), ("project_id", "revision", "version_id", "shot_key", "request_id"), True, True),
    "short-drama-shot-lock": (_CONTENT, "POST", "/api/gen/short-drama/conversation/script/shot/lock", ("project_id", "revision", "version_id", "shot_key", "locked", "request_id"), ("project_id", "revision", "version_id", "shot_key", "locked", "request_id"), True, True),
    "short-drama-shot-structure": (_CONTENT, "POST", "/api/gen/short-drama/conversation/script/shot/structure", ("project_id", "revision", "version_id", "shot_key", "action", "instruction", "request_id"), ("project_id", "revision", "version_id", "shot_key", "action", "request_id"), True, True),
    "short-drama-script-restore": (_CONTENT, "POST", "/api/gen/short-drama/conversation/script/restore", ("project_id", "revision", "version_id", "request_id"), ("project_id", "revision", "version_id", "request_id"), True, True),
    "short-drama-script-lock": (_CONTENT, "POST", "/api/gen/short-drama/conversation/script/lock", ("project_id", "revision", "request_id"), ("project_id", "revision", "request_id"), True, True),
    "short-drama-character-studio": (_CONTENT, "GET", "/api/gen/short-drama/character-studio?{query}", ("project_id",), ("project_id",), False, False),
    "short-drama-character-profile": (_CONTENT, "POST", "/api/gen/short-drama/character-studio/profile", ("project_id", "project_revision", "character_key", "identity_text", "personality", "appearance_prompt", "wardrobe_prompt", "name"), ("project_id", "project_revision", "character_key", "identity_text", "personality", "appearance_prompt", "wardrobe_prompt"), True, False),
    "short-drama-character-avatar": (_CONTENT, "POST", "/api/gen/short-drama/character-studio/bind-avatar", ("project_id", "project_revision", "character_key", "avatar_id"), ("project_id", "project_revision", "character_key", "avatar_id"), True, False),
    "short-drama-character-reference-select": (_CONTENT, "POST", "/api/gen/short-drama/select-character-reference", ("project_id", "revision", "character_key", "source", "asset_job_id", "asset_url", "filename", "image_data"), ("project_id", "revision", "character_key", "source"), True, False),
    "short-drama-scene-graph": (_CONTENT, "GET", "/api/gen/short-drama/asset-graph/scenes?{query}", ("project_id",), ("project_id",), False, False),
    "short-drama-scene-sync": (_CONTENT, "POST", "/api/gen/short-drama/asset-graph/sync", ("project_id", "graph_revision"), ("project_id", "graph_revision"), True, False),
    "short-drama-scene-create": (_CONTENT, "POST", "/api/gen/short-drama/asset-graph/scenes", ("project_id", "graph_revision", "name", "description", "shot_keys"), ("project_id", "graph_revision", "name", "description", "shot_keys"), True, False),
    "short-drama-scene-update": (_CONTENT, "POST", "/api/gen/short-drama/asset-graph/scenes/update", ("project_id", "graph_revision", "scene_key", "name", "description", "shot_keys"), ("project_id", "graph_revision", "scene_key", "name", "description", "shot_keys"), True, False),
    "short-drama-scene-bind-shot": (_CONTENT, "POST", "/api/gen/short-drama/asset-graph/scenes/bind-shot", ("project_id", "graph_revision", "scene_key", "shot_key"), ("project_id", "graph_revision", "scene_key", "shot_key"), True, False),
    "short-drama-scene-delete": (_CONTENT, "POST", "/api/gen/short-drama/asset-graph/scenes/delete", ("project_id", "graph_revision", "scene_key"), ("project_id", "graph_revision", "scene_key"), True, False),
    "short-drama-scene-restore": (_CONTENT, "POST", "/api/gen/short-drama/asset-graph/scenes/restore", ("project_id", "graph_revision", "scene_key"), ("project_id", "graph_revision", "scene_key"), True, False),
    "short-drama-scene-reference": (_CONTENT, "POST", "/api/gen/short-drama/asset-graph/scenes/reference", ("project_id", "graph_revision", "scene_key", "source", "asset_job_id", "asset_url", "filename", "image_data", "prompt", "reference_source"), ("project_id", "graph_revision", "scene_key", "source"), True, False),
    "short-drama-scene-lock": (_CONTENT, "POST", "/api/gen/short-drama/asset-graph/scenes/lock", ("project_id", "graph_revision", "scene_key"), ("project_id", "graph_revision", "scene_key"), True, False),
    "short-drama-autodraft": (_CONTENT, "GET", "/api/gen/short-drama/autodraft?{query}", ("project_id",), ("project_id",), False, False),
    "short-drama-autodraft-render-start": (_CONTENT, "POST", "/api/gen/short-drama/autodraft/jobs", ("project_id", "plan_id", "request_id"), ("project_id", "plan_id", "request_id"), True, True),
    "short-drama-autodraft-render-status": (_CONTENT, "GET", "/api/gen/short-drama/autodraft/jobs/{job_id}?project_id={project_id}", ("project_id", "job_id"), ("project_id", "job_id"), False, False),
    "short-drama-legacy-media-recover": (_CONTENT, "POST", "/api/gen/short-drama/autodraft/legacy-media/recover", ("project_id",), ("project_id",), True, False),
    "short-drama-provider-version-select": (_CONTENT, "POST", "/api/gen/short-drama/autodraft/provider-version/select", ("project_id", "shot_key", "version_id"), ("project_id", "shot_key", "version_id"), True, False),
    "short-drama-refinement": (_CONTENT, "GET", "/api/gen/short-drama/refinement?{query}", ("project_id",), ("project_id",), False, False),
    "short-drama-refinement-status": (_CONTENT, "GET", "/api/gen/short-drama/refinement/jobs/{job_id}?project_id={project_id}", ("project_id", "job_id"), ("project_id", "job_id"), False, False),
    "short-drama-refinement-preview": (_CONTENT, "POST", "/api/gen/short-drama/refinement/changes/preview", ("project_id", "shot_key", "replacement_provider_version_id"), ("project_id", "shot_key"), True, False),
    "short-drama-refinement-adopt": (_CONTENT, "POST", "/api/gen/short-drama/refinement/candidates/adopt", ("project_id", "shot_key", "source_version_id", "replacement_provider_version_id", "request_id"), ("project_id", "shot_key", "source_version_id", "replacement_provider_version_id", "request_id"), True, True),
    "short-drama-refinement-reassemble-candidates": (_CONTENT, "POST", "/api/gen/short-drama/refinement/candidates/reassemble", ("project_id", "version_id", "request_id"), ("project_id", "version_id", "request_id"), True, True),
    "short-drama-refinement-start": (_CONTENT, "POST", "/api/gen/short-drama/refinement/jobs", ("project_id", "shot_key", "source_version_id", "replacement_provider_version_id", "request_id"), ("project_id", "shot_key", "request_id"), True, True),
    "short-drama-refinement-issue": (_CONTENT, "POST", "/api/gen/short-drama/refinement/issues", ("project_id", "version_id", "shot_key", "issue_code", "message"), ("project_id", "version_id", "shot_key"), True, False),
    "short-drama-refinement-keep-original": (_CONTENT, "POST", "/api/gen/short-drama/refinement/issues/keep-original", ("project_id", "version_id", "shot_key"), ("project_id", "version_id", "shot_key"), True, False),
    "short-drama-refinement-media": (_CONTENT, "POST", "/api/gen/short-drama/refinement/media-preference", ("project_id", "mode"), ("project_id", "mode"), True, False),
    "short-drama-refinement-reassemble": (_CONTENT, "POST", "/api/gen/short-drama/refinement/reassemble", ("project_id", "version_id", "request_id"), ("project_id", "version_id", "request_id"), True, True),
    "short-drama-refinement-confirm": (_CONTENT, "POST", "/api/gen/short-drama/refinement/confirm", ("project_id", "version_id", "checklist", "source_hashes"), ("project_id", "version_id", "checklist", "source_hashes"), True, False),
    "short-drama-refinement-restore": (_CONTENT, "POST", "/api/gen/short-drama/refinement/restore", ("project_id", "version_id"), ("project_id", "version_id"), True, False),
}
WEB_PARITY_REMAINING = {}
_ACTION_INPUTS.update({action: fields for action, (_, _, _, fields, _, _, _) in WEB_PARITY_ACTIONS.items()})
CONFIRMATION_ACTIONS = frozenset(set(CONFIRMATION_ACTIONS) | {
    action for action, (_, _, _, _, _, confirm, _) in WEB_PARITY_ACTIONS.items() if confirm
})

_ACTION_PURPOSES = {
    "account": "读取当前黄雀账号与点数", "channels": "读取可用渠道", "pricing": "读取实时价格",
    "director-capability": "读取编导与数字人一键生成的完整 CLI 覆盖契约",
    "digital-human-oneclick-capability": "读取普通数字人一键生成的实时能力、限制和供应商状态",
    "digital-human-oneclick-plan": "按文案或完整录音生成服务端冻结时间轴方案",
    "digital-human-oneclick-consent": "保存与 plan_digest 绑定的本人照片、声音和素材授权",
    "digital-human-oneclick-start": "报价后以唯一 request_id 启动服务端数字人运行",
    "digital-human-oneclick-status": "读取本人数字人运行、子任务、扣点退款与成片状态",
    "digital-human-oneclick-recover": "仅恢复原运行中可恢复的失败步骤",
    "digital-human-oneclick-abandon": "放弃原运行的后续恢复并保留账务审计状态",
    "digital-human-oneclick-history": "读取本人已完成的数字人成片历史",
    "director-script-generate": "按主站编导规则生成结构化脚本与分镜",
    "director-breakdown": "拆解抖音或小红书作品链接并生成分镜或反推提示词",
    "director-breakdown-upload": "上传本地图片或视频并反推可复用提示词",
    "director-scene-image-generate": "根据编导分镜画面描述生成图片",
    "director-scene-video-generate": "根据编导分镜画面描述生成剧情视频",
    "director-scene-talking-generate": "根据编导口播分镜生成人物口播镜头",
    "director-chat": "调用编导顾客助手生成可追踪的零点数对话任务",
    "director-produce": "确认编导助手已经报价并冻结的脚本生产单",
    "director-workflows": "读取本人编导工作流列表",
    "director-workflow-create": "从本人已完成任务或分镜创建编导工作流",
    "director-workflow": "读取本人编导工作流与当前 revision",
    "director-storyboard-update": "按 revision 保存本人编导分镜",
    "director-storyboard-export": "导出本人编导分镜 Markdown",
    "director-production-plan": "冻结本人编导工作流生产方案",
    "director-production-start": "报价后启动冻结的编导生产方案",
    "director-production-status": "读取编导生产任务与账务状态",
    "director-production-recover": "按原 request_id 恢复结果未知的编导生产",
    "director-remake-plan": "冻结本人编导同款复刻方案",
    "director-remake-start": "报价后启动冻结的同款复刻方案",
    "director-remake-status": "读取同款复刻任务与账务状态",
    "director-remake-recover": "按原 request_id 恢复结果未知的同款复刻",
    "ip12-projects": "读取本人 IP12 项目", "ip12-project": "读取本人 IP12 项目详情",
    "ip12-report": "读取本人 IP12 报告", "ip12-delete": "删除本人 IP12 项目",
    "canvas-list": "读取本人画布", "canvas-get": "读取本人画布详情",
    "canvas-delete": "删除本人创建的画布",
    "tasks": "读取本人任务记录", "task": "读取本人任务详情", "assets": "读取本人资产", "voices": "读取可用音色",
    "asset-delete": "删除本人自产资产（单条或批量）",
    "video-compose-delete": "删除本人一键成片项目",
    "digital-presenter-delete": "删除本人画布中的数字人口播项目",
    "short-drama-create": "创建本人短剧项目",
    "short-drama-delete": "删除本人短剧项目",
    "short-drama-advisor": "调用短剧顾问协商立项信息",
    "short-drama-character-reference-generate": "报价后生成短剧角色标准图",
    "short-drama-character-reference-confirm": "锁定已生成的角色标准图版本",
    "short-drama-preflight-plan": "生成短剧制作体检方案",
    "short-drama-preflight-confirm": "确认当前短剧制作体检方案",
    "short-drama-autodraft-preflight": "编译单镜头真实视频供应商请求",
    "short-drama-autodraft-quote": "获取单镜头真实视频报价",
    "short-drama-autodraft-start": "确认单镜头报价并启动原任务",
    "short-drama-autodraft-status": "读取单镜头真实视频任务状态",
    "short-drama-delivery-quote": "获取短剧正式交付报价",
    "short-drama-delivery-start": "确认正式交付报价并启动原任务",
    "short-drama-delivery-status": "读取短剧正式交付任务状态",
    "short-drama-completion-readiness": "读取短剧完成门禁与阻塞项",
    "short-drama-completion": "读取短剧不可变完成快照",
    "short-drama-completion-confirm": "确认短剧不可逆完成交付",
    "leads-delete": "删除本人线索跟进记录",
    "digital-ip-create": "创建本人数字 IP 项目",
    "digital-ip-update": "更新本人数字 IP 项目",
    "digital-ip-delete": "删除本人数字 IP 项目",
    "video-avatar-create": "上传本人照片创建数字人形象",
    "voice-clone-create": "用本人样音创建或重新录制个人克隆音色",
    "voice-clone-status": "读取个人克隆音色处理状态",
    "image-generate": "生成图片", "video-generate": "生成视频", "video-lipsync": "让本人原视频匹配新口播音频",
    "audio-generate": "生成音频", "text-video-generate": "根据主题或完整文案生成成片",
    "matrix-template-capability": "读取模板成片服务状态",
    "matrix-template-templates": "读取模板成片视觉模板",
    "matrix-template-generate": "使用平台素材库创建模板成片",
    "matrix-template-batch-generate": "使用同一文案和模板批量创建 2-5 条模板成片",
    "text-video-avatar-import": "导入文案成片口播人物图片",
    "text-video-plan": "生成可选择的文案成片口播分镜方案",
    "canvas-agent-plan": "为画布生成可确认的操作方案", "canvas-ops": "写入本人画布操作",
}

# Discovery is descriptive only.  `action_plan` below remains the sole input
# validator and route builder.  Keep this subset explicit because agents need
# usable media/Canvas schemas, not the old field-type-only placeholder.
_ID_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 160}
_INT_ID_SCHEMA = {"type": "integer", "minimum": 1, "maximum": 2**63 - 1}
_IMAGE_UPLOAD_SCHEMA = {"type": "string", "pattern": "^img_[0-9a-f]{32}$"}
_VIDEO_UPLOAD_SCHEMA = {"type": "string", "pattern": "^vid_[0-9a-f]{32}$"}
_AUDIO_UPLOAD_SCHEMA = {"type": "string", "pattern": "^aud_[0-9a-f]{32}$"}
_VOICE_SLOT_ID_PATTERN = "^[A-Za-z][A-Za-z0-9_-]{1,87}$"
_VOICE_SLOT_ID_RE = re.compile(_VOICE_SLOT_ID_PATTERN)

# Keep discovery and executable planning on one channel matrix.  The public
# CLI carries an identical table and the cross-package contract test prevents
# the two distributions from drifting apart.
_VIDEO_CHANNEL_RULES = {
    "grok": {
        "ratios": ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"],
        "duration": [1, 15], "seconds": [],
        "resolutions": ["480p", "720p"],
        "models": ["grok-imagine-video", "grok-imagine-video-1.5"],
        "reference_max": 7, "generate_audio": False,
        "default_ratio": "16:9", "default_duration": 10,
        "default_resolution": "720p", "default_model": "grok-imagine-video",
        "reference_resolutions": ["720p"],
        "reference_required_models": ["grok-imagine-video-1.5"],
    },
    "micro": {
        "ratios": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16", "adaptive"],
        "duration": [4, 15], "seconds": [],
        "resolutions": ["480p", "720p", "1080p"], "models": [],
        "reference_max": 9, "generate_audio": True,
        "default_ratio": "9:16", "default_duration": 5,
        "default_resolution": "720p", "default_model": "",
    },
    "omni": {
        "ratios": ["9:16", "16:9"], "duration": [3, 10], "seconds": [],
        "resolutions": ["720p"], "models": [],
        "reference_max": 6, "generate_audio": False,
        "default_ratio": "16:9", "default_duration": 5,
        "default_resolution": "720p", "default_model": "",
    },
    "minimax": {
        "ratios": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16", "adaptive"],
        "duration": [4, 15], "seconds": [],
        "resolutions": ["2k"], "models": [],
        "reference_max": 5, "generate_audio": False,
        "default_ratio": "9:16", "default_duration": 5,
        "default_resolution": "2k", "default_model": "",
    },
    "sora": {
        "ratios": ["9:16", "16:9"], "duration": [], "seconds": [4, 8, 12],
        "resolutions": ["720p", "1024p", "1080p"],
        "models": ["sora-2", "sora-2-pro"],
        "reference_max": 1, "generate_audio": False,
        "default_ratio": "9:16", "default_seconds": 4,
        "default_resolution": "720p", "default_model": "sora-2",
        "model_resolutions": {
            "sora-2": ["720p"],
            "sora-2-pro": ["720p", "1024p", "1080p"],
        },
    },
}


def _video_channel_then(rule):
    properties = {
        "ratio": {"enum": list(rule["ratios"])},
        "resolution": {"enum": list(rule["resolutions"])},
        "reference_upload_ids": {
            "type": "array", "minItems": 1,
            "maxItems": int(rule["reference_max"]),
        },
    }
    forbidden = []
    if rule["duration"]:
        properties["duration"] = {
            "type": "integer", "minimum": rule["duration"][0],
            "maximum": rule["duration"][1],
        }
        forbidden.append("seconds")
    else:
        properties["seconds"] = {"type": "integer", "enum": list(rule["seconds"])}
        forbidden.append("duration")
    if rule["models"]:
        properties["model"] = {"type": "string", "enum": list(rule["models"])}
    else:
        forbidden.append("model")
    if not rule["generate_audio"]:
        forbidden.append("generate_audio")
    result = {"properties": properties}
    if forbidden:
        result["not"] = {"anyOf": [{"required": [field]} for field in forbidden]}
    return result


def _video_channel_schema():
    clauses = []
    for channel, rule in _VIDEO_CHANNEL_RULES.items():
        clauses.append({
            "if": {"properties": {"channel": {"const": channel}}, "required": ["channel"]},
            "then": _video_channel_then(rule),
        })
    clauses.append({
        "if": {"not": {"required": ["channel"]}},
        "then": _video_channel_then(_VIDEO_CHANNEL_RULES["grok"]),
    })
    grok_selector = {"anyOf": [
        {"not": {"required": ["channel"]}},
        {"properties": {"channel": {"const": "grok"}}, "required": ["channel"]},
    ]}
    clauses.append({
        "if": {"allOf": [grok_selector, {"required": ["reference_upload_ids"]}]},
        "then": {"properties": {"resolution": {"enum": ["720p"]}}},
    })
    clauses.append({
        "if": {"allOf": [grok_selector, {
            "properties": {"model": {"const": "grok-imagine-video-1.5"}},
            "required": ["model"],
        }]},
        "then": {
            "required": ["reference_upload_ids"],
            "properties": {"resolution": {"enum": ["720p"]}},
        },
    })
    sora_selector = {
        "properties": {"channel": {"const": "sora"}}, "required": ["channel"],
    }
    for model, resolutions in _VIDEO_CHANNEL_RULES["sora"]["model_resolutions"].items():
        clauses.append({
            "if": {"allOf": [sora_selector, {
                "properties": {"model": {"const": model}}, "required": ["model"],
            }]},
            "then": {"properties": {"resolution": {"enum": list(resolutions)}}},
        })
    clauses.append({
        "if": {"allOf": [sora_selector, {"not": {"required": ["model"]}}]},
        "then": {"properties": {"resolution": {"enum": ["720p"]}}},
    })
    return clauses


_MEDIA_SCHEMAS = {
    "image-generate": {
        "required": ["prompt"], "properties": {
            "prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
            "provider": {"type": "string", "enum": ["openai", "xiaole", "seedream", "banana"]},
            "ratio": {"type": "string", "enum": ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]},
            "quality": {"type": "string", "enum": ["std", "hd"]},
            "count": {"type": "integer", "minimum": 1, "maximum": 4},
            "variant": {"type": "string", "enum": ["std", "pro"]},
            "model": {"type": "string", "enum": ["nb2", "pro"]},
            "image_upload_id": _IMAGE_UPLOAD_SCHEMA, "mask_upload_id": _IMAGE_UPLOAD_SCHEMA,
            "reference_upload_ids": {"type": "array", "minItems": 1, "maxItems": 16,
                                     "items": _IMAGE_UPLOAD_SCHEMA},
        },
        "constraints": [
            "provider-specific reference_upload_ids limit: openai=16, seedream=10, xiaole=4, banana=14",
            "image_upload_id and reference_upload_ids are mutually exclusive",
            "mask_upload_id requires image_upload_id, provider=openai, and count=1",
            "model is only for banana; variant is only for seedream; banana count is 1, 2, or 4",
        ],
    },
    "video-generate": {
        "required": ["prompt"], "properties": {
            "prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
            "channel": {"type": "string", "enum": ["grok", "micro", "omni", "minimax", "sora"]},
            "ratio": {"type": "string", "enum": ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9", "adaptive"]},
            "duration": {"type": "integer", "minimum": 1, "maximum": 15},
            "seconds": {"type": "integer", "enum": [4, 8, 12]},
            "resolution": {"type": "string", "enum": ["480p", "720p", "1024p", "1080p", "2k"]},
            "model": {"type": "string", "enum": ["grok-imagine-video", "grok-imagine-video-1.5", "sora-2", "sora-2-pro"]},
            "generate_audio": {"type": "boolean"},
            "reference_upload_ids": {"type": "array", "minItems": 1, "maxItems": 9,
                                     "items": _IMAGE_UPLOAD_SCHEMA},
        },
        "allOf": _video_channel_schema(),
        "x-hq-channel-rules": _VIDEO_CHANNEL_RULES,
        "constraints": [
            "reference_upload_ids limit: grok=7, micro=9, omni=6, minimax=5",
            "channel=minimax accepts only resolution=2k for new tasks",
            "resolution=2k is only valid when channel=minimax",
            "channel-specific ratio, duration/seconds, resolution, model, and reference rules are machine-readable in input_schema.allOf",
            "sora uses model=sora-2|sora-2-pro, seconds=4|8|12, ratio=9:16|16:9, resolution=720p|1024p|1080p, and exactly one reference when supplied",
            "sora rejects duration and generate_audio; seconds is only for sora; model is otherwise only for grok",
            "generate_audio is only a boolean for micro",
        ],
    },
    "video-lipsync": {
        "required": ["video_asset_id", "audio_asset_id"],
        "properties": {
            "video_asset_id": _INT_ID_SCHEMA,
            "audio_asset_id": _INT_ID_SCHEMA,
            "quality": {"type": "string", "enum": ["speed", "precision"]},
            "dynamic_duration": {"type": "boolean"},
        },
        "constraints": [
            "video_asset_id and audio_asset_id must be completed assets owned by this account",
            "quality defaults to speed; precision costs twice as many points",
            "dynamic_duration defaults to false to preserve the source performance timing",
            "the source video must be 1-300 seconds",
        ],
    },
    "audio-generate": {
        "required": ["text"], "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": 1000},
            "voice": {"type": "string", "minLength": 1, "maxLength": 128},
            "speed": {"type": "number", "minimum": 0.5, "maximum": 2},
            "pitch": {"type": "integer", "minimum": -12, "maximum": 12},
            "volume": {"type": "integer", "minimum": -50, "maximum": 100},
        }, "constraints": ["speed is rounded to one decimal place"],
    },
    "text-video-generate": {
        "required": ["text", "template", "style", "voice"], "properties": {
            "text": {"type": "string", "minLength": 2, "maxLength": 1000},
            "template": {"type": "string", "minLength": 1, "maxLength": 240},
            "mode": {"type": "string", "enum": ["generate", "fixed"]},
            "style": {"type": "string", "minLength": 1, "maxLength": 80},
            "voice": {"type": "string", "minLength": 1, "maxLength": 200},
            "speech_rate": {"type": "number", "minimum": 0.5, "maximum": 2.0},
            "talking_material": {"type": "object"},
        },
        "constraints": [
            "template, style, and voice must come from the matching text-video read capabilities",
            "mode defaults to generate; fixed preserves the supplied copy and automatically splits scenes",
            "the signed CLI quote carries the native quote; final submission revalidates it before deduction",
            "talking_material must reference an active plan and owner-scoped avatar assets",
        ],
    },
    "matrix-template-generate": {
        "required": ["top_text", "bottom_text", "template_id"], "properties": {
            "top_text": {"type": "string", "minLength": 2, "maxLength": 60},
            "bottom_text": {"type": "string", "minLength": 2, "maxLength": 80},
            "template_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"},
            "font_family": {"type": "string", "maxLength": 80},
        },
        "constraints": [
            "template_id must come from matrix-template-templates",
            "font_family is optional and must come from matrix-template-templates fonts",
            "duration is automatic, BGM is enabled, and only approved platform-library media is used",
        ],
    },
    "matrix-template-batch-generate": {
        "required": ["top_text", "bottom_text", "template_id", "count"], "properties": {
            "top_text": {"type": "string", "minLength": 2, "maxLength": 60},
            "bottom_text": {"type": "string", "minLength": 2, "maxLength": 80},
            "template_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"},
            "font_family": {"type": "string", "maxLength": 80},
            "count": {"type": "integer", "minimum": 2, "maximum": 5},
        },
        "constraints": [
            "template_id and optional font_family must come from matrix-template-templates",
            "count creates 2-5 independent jobs under one total quote and one confirmation",
            "HyperFrames and other font-locked templates are single-only; use matrix-template-generate",
            "duration is automatic, BGM is enabled, and only approved platform-library media is used",
        ],
    },
    "text-video-avatar-import": {
        "required": ["image_upload_id"],
        "properties": {"image_upload_id": _IMAGE_UPLOAD_SCHEMA},
        "constraints": ["image_upload_id must be a current owner-scoped image-upload result"],
    },
    "text-video-plan": {
        "required": ["text", "template", "style", "voice"], "properties": {
            "text": {"type": "string", "minLength": 2, "maxLength": 1000},
            "template": {"type": "string", "minLength": 1, "maxLength": 240},
            "mode": {"type": "string", "enum": ["generate", "fixed"]},
            "style": {"type": "string", "minLength": 1, "maxLength": 80},
            "voice": {"type": "string", "minLength": 1, "maxLength": 200},
            "speech_rate": {"type": "number", "minimum": 0.5, "maximum": 2.0},
            "ratio": {"type": "number", "minimum": 0.1, "maximum": 0.5},
        },
        "constraints": ["creates an expiring owner-scoped plan and requires explicit confirmation"],
    },
    "canvas-create": {
        "required": ["name"], "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 48},
            "prompt": {"type": "string", "maxLength": 2000},
        }, "constraints": ["a non-empty prompt becomes the first text node"],
    },
    "canvas-agent-plan": {
        "required": ["prompt", "project_id", "snapshot_digest", "scope", "nodes", "edges", "selected_node_ids"],
        "properties": {
            "prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
            "project_id": {"type": "string", "pattern": "^(local|collab):[A-Za-z0-9_-]{1,120}$"},
            "snapshot_digest": {"type": "string", "pattern": "^[a-f0-9]{8,32}$"},
            "scope": {"type": "string", "enum": ["local", "collab"]},
            "nodes": {"type": "array", "maxItems": 60}, "edges": {"type": "array", "maxItems": 120},
            "selected_node_ids": {"type": "array", "maxItems": 30}, "history": {"type": "array", "maxItems": 10},
            "page_context": {"type": "object"}, "ip12_context": {"type": "object"},
        },
        "constraints": [
            "project_id prefix must match scope; nodes and edges may not reference unknown or duplicate node IDs",
            "node text is capped at 30,000 characters total; history is at most 10 user/assistant messages",
            "page_context is only the Huangque Canvas page; media data, blob URLs, and base64 are rejected",
        ],
    },
    "canvas-ops": {
        "required": ["board_id", "base_version", "op_id", "ops"], "properties": {
            "board_id": _ID_SCHEMA, "base_version": _INT_ID_SCHEMA,
            "op_id": {"type": "string", "pattern": "^hqcli-[A-Za-z0-9_-]{11,122}$"},
            "ops": {"type": "array", "minItems": 1, "maxItems": 12},
        },
        "constraints": [
            "only node.create, node.patch, and edge.create are accepted",
            "created node types are text, gen, or video; coordinates are 0-100000",
            "deletion, board replacement, generated output, membership, and script operations are rejected",
        ],
    },
}

_TALKING_FIELDS = {
    "ratio": {"type": "string", "enum": ["9:16", "16:9", "1:1", "4:5", "5:4"]},
    "motion": {"type": "string", "enum": ["low", "medium", "high"]}, "subtitle": {"type": "boolean"},
    "subtitle_style": {"type": "string", "enum": ["white", "variety", "bar"]},
    "subtitle_position": {"type": "string", "enum": ["top", "upper", "center", "lower", "bottom"]},
}
_MEDIA_SCHEMAS.update({
    "director-script-generate": {
        "required": ["prompt"], "properties": {
            "prompt": {"type": "string", "minLength": 1, "maxLength": 20000},
            "style": {"type": "string", "enum": ["spoken", "story", "recommend"]},
            "duration": {"type": "integer", "enum": [15, 30, 60]},
            "platform": {"type": "string", "enum": ["douyin", "xiaohongshu", "channels"]},
        }, "constraints": ["paid action: quote first, then confirm the identical normalized input"],
    },
    "director-breakdown": {
        "required": [], "properties": {
            "url": {"type": "string", "minLength": 1, "maxLength": 2000},
            "urls": {"type": "array", "minItems": 1, "maxItems": 5,
                     "items": {"type": "string", "minLength": 1, "maxLength": 2000}},
            "mode": {"type": "string", "enum": ["scenes", "reverse_prompt"]},
        }, "oneOf": [{"required": ["url"]}, {"required": ["urls"]}],
        "constraints": ["supports public Douyin or Xiaohongshu links only",
                        "reverse_prompt accepts exactly one URL",
                        "paid action: quote first, then confirm the identical normalized input"],
    },
    "director-scene-image-generate": {
        "required": ["scenes"], "properties": {
            "scenes": {"type": "array", "minItems": 1, "maxItems": 8, "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "scene": {"type": "string", "maxLength": 2000},
                    "line": {"type": "string", "maxLength": 2000},
                    "dur": {"type": "number", "exclusiveMinimum": 0, "maximum": 180},
                },
            }},
            "ratio": {"type": "string", "enum": ["9:16", "16:9", "1:1", "4:5", "5:4"]},
            "quality": {"type": "string", "enum": ["standard", "hd"]},
        }, "constraints": [
            "at least one scene must contain a non-empty scene description",
            "matches the Director page scene-image path",
            "paid action: quote first, then confirm the identical normalized input",
        ],
    },
    "canvas-list": {"required": [], "properties": {
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        "offset": {"type": "integer", "minimum": 0, "maximum": 100000},
    }, "constraints": []},
    "canvas-get": {"required": ["board_id"], "properties": {"board_id": _ID_SCHEMA}, "constraints": []},
    "voice-clone-create": {"required": ["slot_id", "name", "audio_upload_id"], "properties": {
        "slot_id": {"type": "string", "pattern": _VOICE_SLOT_ID_PATTERN},
        "name": {"type": "string", "minLength": 1, "maxLength": 40},
        "audio_upload_id": _AUDIO_UPLOAD_SCHEMA,
    }, "constraints": [
        "sample audio is private to the current account and should contain 30-60 seconds of continuous, clear, single-speaker speech",
        "long silence, music, and noise do not count as effective speech; file duration alone is not sufficient",
        "after submission, poll voice-clone-status for the same slot_id until ready or failed and inspect clone_error before any new operation",
        "reusing a ready slot replaces that personal cloned voice and requires explicit confirmation",
    ]},
    "voice-clone-status": {"required": ["slot_id"], "properties": {
        "slot_id": {"type": "string", "pattern": _VOICE_SLOT_ID_PATTERN},
    }, "constraints": []},
    "digital-ip-text-generate": {"required": ["text", "voice"], "properties": {
        "avatar_id": _INT_ID_SCHEMA, "image_upload_id": {**_IMAGE_UPLOAD_SCHEMA, "title": "人物照片"},
        "text": {"type": "string", "minLength": 1, "maxLength": 1000},
        "voice": {"type": "string", "minLength": 1, "maxLength": 128}, **_TALKING_FIELDS},
        "constraints": ["provide one ready account avatar_id or one private image_upload_id; output is fixed at 1080p"]},
    "digital-ip-audio-generate": {"required": [], "properties": {
        "avatar_id": _INT_ID_SCHEMA, "image_upload_id": {**_IMAGE_UPLOAD_SCHEMA, "title": "人物照片"},
        "audio_file": {"type": "string", "minLength": 1, "maxLength": 500},
        "audio_upload_id": {**_AUDIO_UPLOAD_SCHEMA, "title": "本人口播音频"}, **_TALKING_FIELDS},
        "constraints": ["provide one avatar_id or image_upload_id and one owned audio_file or audio_upload_id; output is fixed at 1080p"]},
    "digital-ip-batch-generate": {"required": ["avatars", "text", "voice"], "properties": {
        "avatars": {"type": "array", "minItems": 2, "maxItems": 5, "uniqueItems": True, "items": {
            "type": "object", "additionalProperties": False, "required": ["avatar_id"], "properties": {
                "avatar_id": _INT_ID_SCHEMA, "label": {"type": "string", "minLength": 1, "maxLength": 60},
            }}},
        "text": {"type": "string", "minLength": 1, "maxLength": 1000},
        "voice": {"type": "string", "minLength": 1, "maxLength": 128}, **_TALKING_FIELDS},
        "constraints": ["avatars contains 2-5 distinct ready account avatar IDs, each optionally labelled up to 60 characters; output is fixed at 1080p"]},
    "cinematic-open-generate": {"required": ["prompt"], "properties": {
        "avatar_id": _INT_ID_SCHEMA, "avatar_ids": {"type": "array", "minItems": 1, "maxItems": 3, "uniqueItems": True, "items": _INT_ID_SCHEMA},
        "prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
        "ratio": {"type": "string", "enum": ["9:16", "16:9", "1:1"]},
        "duration": {"type": "integer", "minimum": 4, "maximum": 15}, "enhance_prompt": {"type": "boolean"},
        "reference_image_upload_ids": {"type": "array", "minItems": 1, "maxItems": 8, "items": _IMAGE_UPLOAD_SCHEMA},
        "reference_video_upload_ids": {"type": "array", "minItems": 1, "maxItems": 3, "items": _VIDEO_UPLOAD_SCHEMA},
    }, "constraints": ["provide avatar_id or avatar_ids, never both; avatars and image references share 9 slots; output is fixed at 720p"]},
    "cinematic-motion-generate": {"required": ["avatar_id", "reference_video_upload_ids"], "properties": {
        "avatar_id": _INT_ID_SCHEMA, "reference_video_upload_ids": {"type": "array", "minItems": 1, "maxItems": 1, "items": _VIDEO_UPLOAD_SCHEMA},
        "ratio": {"type": "string", "enum": ["9:16", "16:9", "1:1"]},
    }, "constraints": ["avatar_id must be a ready cinematic avatar; output is fixed at 720p"]},
    "tryon-fast-generate": {"required": ["person_image_upload_id", "clothes_upload_id"], "properties": {
        "person_image_upload_id": {**_IMAGE_UPLOAD_SCHEMA, "title": "人物图片"},
        "clothes_upload_id": {**_IMAGE_UPLOAD_SCHEMA, "title": "服装图片"},
        "seconds": {"type": "integer", "minimum": 5, "maximum": 15},
    }, "constraints": ["both uploads are private images owned by this account; seconds defaults to 6"]},
    "tryon-classic-generate": {"required": ["person_video_upload_id"], "properties": {
        "person_video_upload_id": {**_VIDEO_UPLOAD_SCHEMA, "title": "人物视频"},
        "clothes_upload_id": {**_IMAGE_UPLOAD_SCHEMA, "title": "服装图片"},
        "background_upload_id": {**_IMAGE_UPLOAD_SCHEMA, "title": "背景图片"},
        "seconds": {"type": "integer", "minimum": 1, "maximum": 6},
    }, "constraints": ["provide clothes_upload_id, background_upload_id, or both; seconds defaults to 6"]},
    "video-avatars": {"required": [], "properties": {
        "limit": {"type": "integer", "minimum": 1, "maximum": 120},
    }, "constraints": []},
    "video-avatar-create": {"required": ["image_data"], "properties": {
        "image_data": {"type": "string", "minLength": 32, "maxLength": 12 * 1024 * 1024,
                       "description": "本人真人照片的 data URL（jpg/png/webp，正脸清晰、光线充足）"},
        "name": {"type": "string", "minLength": 1, "maxLength": 40},
    }, "constraints": [
        "照片必须是本人或获得授权的人像；创建后可在数字人口播/剧情视频中反复使用",
        "创建数字人形象按 avatar.create 计费，先报价、确认后扣点提交",
        "提交后轮询 video-avatars 直到 status 变为 ready（约 30 秒）",
    ]},
    "video-compose-projects": {"required": [], "properties": {}, "constraints": []},
    "video-compose-project": {"required": ["project_id"], "properties": {
        "project_id": {"type": "string", "pattern": "^compose_[0-9a-f]{32}$"},
    }, "constraints": []},
    "video-compose-create": {"required": ["source_asset_id"], "properties": {
        "source_asset_id": _INT_ID_SCHEMA,
    }, "constraints": ["source_asset_id must be a completed video asset owned by this account"]},
    "video-compose-analyze": {"required": ["project_id", "expected_revision"], "properties": {
        "project_id": {"type": "string", "pattern": "^compose_[0-9a-f]{32}$"}, "expected_revision": _INT_ID_SCHEMA,
    }, "constraints": ["analysis is non-destructive and uses the expected current project revision"]},
    "video-compose-review": {"required": ["project_id", "expected_revision", "decisions"], "properties": {
        "project_id": {"type": "string", "pattern": "^compose_[0-9a-f]{32}$"}, "expected_revision": _INT_ID_SCHEMA,
        "decisions": {"type": "object", "minProperties": 1, "maxProperties": 200,
                      "additionalProperties": {"type": "string", "enum": ["keep", "remove"]}},
    }, "constraints": []},
    "video-compose-render": {"required": ["project_id", "expected_revision"], "properties": {
        "project_id": {"type": "string", "pattern": "^compose_[0-9a-f]{32}$"}, "expected_revision": _INT_ID_SCHEMA,
    }, "constraints": ["render uses the confirmed EDL for this project revision"]},
    "digital-presenter-capability": {"required": [], "properties": {}, "constraints": []},
    "digital-presenter-project": {"required": ["board_id", "project_id"], "properties": {
        "board_id": _ID_SCHEMA, "project_id": {"type": "string", "pattern": "^dp_[0-9a-f]{32}$"},
    }, "constraints": []},
    "digital-presenter-create": {"required": ["board_id", "request_id"], "properties": {
        "board_id": _ID_SCHEMA, "request_id": {"type": "string", "pattern": "^[A-Za-z0-9._:-]{8,128}$"},
        "title": {"type": "string", "minLength": 1, "maxLength": 80}, "script_text": {"type": "string", "maxLength": 20000},
        "ratio": {"type": "string", "enum": ["9:16", "16:9"]}, "resolution": {"type": "string", "enum": ["1080p"]},
        "voice_key": {"type": "string", "maxLength": 200}, "target_duration": {"type": "integer", "minimum": 30, "maximum": 180},
    }, "constraints": ["request_id is the idempotency key"]},
    "digital-presenter-update": {"required": ["board_id", "project_id", "revision"], "properties": {
        "board_id": _ID_SCHEMA, "project_id": {"type": "string", "pattern": "^dp_[0-9a-f]{32}$"}, "revision": _INT_ID_SCHEMA,
        "title": {"type": "string", "minLength": 1, "maxLength": 80}, "script_text": {"type": "string", "maxLength": 20000},
        "ratio": {"type": "string", "enum": ["9:16", "16:9"]}, "resolution": {"type": "string", "enum": ["1080p"]},
        "voice_key": {"type": "string", "maxLength": 200}, "target_duration": {"type": "integer", "minimum": 30, "maximum": 180},
    }, "constraints": ["provide at least one editable field and the current revision"]},
    "asset-delete": {"required": ["kind"], "properties": {
        "kind": {"type": "string", "enum": ["image", "audio", "video", "copy", "collect", "leads", "breakdown"]},
        "id": _INT_ID_SCHEMA,
        "keys": {"type": "array", "minItems": 1, "maxItems": 200, "uniqueItems": True,
                 "items": {"type": "string", "minLength": 1, "maxLength": 500}},
    }, "anyOf": [{"required": ["id"]}, {"required": ["keys"]}], "constraints": [
        "deletion is owner-scoped and soft; read the asset before confirming",
        "provide exactly one of id (single delete) or keys (batch 1-200)",
        "avatar kind is not deletable through this action",
    ]},
    "canvas-delete": {"required": ["board_id"], "properties": {
        "board_id": _ID_SCHEMA,
    }, "constraints": ["only the board owner can delete", "deletion is irreversible and always requires confirm=true"]},
    "video-compose-delete": {"required": ["project_id", "expected_revision"], "properties": {
        "project_id": {"type": "string", "pattern": "^compose_[0-9a-f]{32}$"},
        "expected_revision": _INT_ID_SCHEMA,
    }, "constraints": ["soft delete guarded by the current revision"]},
    "digital-presenter-delete": {"required": ["board_id", "project_id", "revision"], "properties": {
        "board_id": _ID_SCHEMA,
        "project_id": {"type": "string", "pattern": "^dp_[0-9a-f]{32}$"},
        "revision": _INT_ID_SCHEMA,
    }, "constraints": ["board_id must be a canvas the account can edit"]},
    "short-drama-create": {"required": ["title", "synopsis", "ratio", "target_duration", "shot_count", "request_id"], "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 80},
        "synopsis": {"type": "string", "minLength": 8, "maxLength": 4000},
        "ratio": {"type": "string", "enum": ["9:16", "16:9"]},
        "target_duration": {"type": "integer", "enum": [30, 45, 60]},
        "shot_count": {"type": "integer", "minimum": 6, "maximum": 10},
        "genre": {"type": "string", "maxLength": 40},
        "visual_style": {"type": "string", "maxLength": 80},
        "request_id": {"type": "string", "pattern": "^[A-Za-z0-9._:-]{8,128}$"},
    }, "constraints": ["request_id is the idempotency key; retry with the same request_id"]},
    "short-drama-delete": {"required": ["project_id", "revision"], "properties": {
        "project_id": _ID_SCHEMA,
        "revision": _INT_ID_SCHEMA,
    }, "constraints": ["soft delete guarded by the current revision"]},
    "leads-delete": {"required": ["lead_ids"], "properties": {
        "lead_ids": {"type": "array", "minItems": 1, "maxItems": 100,
                     "items": {"type": "string", "pattern": "^[0-9a-f]{16,40}$"}},
    }, "constraints": ["only CRM rows owned by the current account are deleted"]},
    "digital-ip-create": {"required": ["title"], "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 80},
    }, "constraints": ["subject to the account's project count limit"]},
    "digital-ip-update": {"required": ["project_id", "revision"], "properties": {
        "project_id": _ID_SCHEMA,
        "revision": _INT_ID_SCHEMA,
        "title": {"type": "string", "minLength": 1, "maxLength": 80},
    }, "constraints": ["revision must match the latest project revision"]},
    "digital-ip-delete": {"required": ["project_id", "revision"], "properties": {
        "project_id": _ID_SCHEMA,
        "revision": _INT_ID_SCHEMA,
    }, "constraints": ["soft delete guarded by the current revision"]},
})

_DIGITAL_HUMAN_RUN_ID_SCHEMA = {
    "type": "string", "pattern": "^dh-run-[A-Za-z0-9._:-]{1,128}$",
}
_DIGITAL_HUMAN_REQUEST_ID_SCHEMA = {
    "type": "string", "pattern": "^[A-Za-z0-9._:-]{8,128}$",
}
_DIGITAL_HUMAN_PLAN_DIGEST_SCHEMA = {
    "type": "string", "pattern": "^[0-9a-f]{64}$",
}
_DIGITAL_HUMAN_MATERIAL_IDS_SCHEMA = {
    "type": "array", "maxItems": 12, "uniqueItems": True,
    "items": {"type": "string", "pattern": "^img_[0-9a-f]{32}$"},
}
_MEDIA_SCHEMAS.update({
    "digital-human-oneclick-capability": {
        "required": [], "properties": {}, "constraints": [],
    },
    "digital-human-oneclick-plan": {
        "required": ["narration_mode"], "properties": {
            "script": {"type": "string", "minLength": 12, "maxLength": 6000},
            "narration_mode": {"type": "string", "enum": ["text", "audio"]},
            "audio_upload_id": {"type": "string", "pattern": "^dha_[0-9a-f]{32}$"},
            "allow_ai_materials": {"type": "boolean"},
            "customer_upload_ids": _DIGITAL_HUMAN_MATERIAL_IDS_SCHEMA,
        },
        "oneOf": [
            {"required": ["script"]}, {"required": ["audio_upload_id"]},
        ],
        "constraints": [
            "text mode requires script; audio mode requires a dedicated digital-human audio upload",
            "customer_upload_ids must belong to the current account",
        ],
    },
    "digital-human-oneclick-consent": {
        "required": [
            "confirmed", "consent_version", "purpose", "run_id",
            "plan_digest", "photo_sha256", "voice_mode", "voice_ref",
            "narration_mode",
        ],
        "properties": {
            "confirmed": {"type": "boolean", "const": True},
            "consent_version": {"type": "string", "const": "digital-human-material-v3"},
            "purpose": {"type": "string", "const": "digital_human_material_v3"},
            "run_id": _DIGITAL_HUMAN_RUN_ID_SCHEMA,
            "plan_digest": _DIGITAL_HUMAN_PLAN_DIGEST_SCHEMA,
            "script": {"type": "string", "maxLength": 6000},
            "photo_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "voice_mode": {"type": "string", "enum": ["existing", "audio"]},
            "voice_ref": {"type": "string", "maxLength": 180},
            "voice_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "narration_mode": {"type": "string", "enum": ["text", "audio"]},
            "audio_upload_id": {"type": "string", "pattern": "^dha_[0-9a-f]{32}$"},
            "allow_ai_materials": {"type": "boolean"},
            "customer_upload_ids": _DIGITAL_HUMAN_MATERIAL_IDS_SCHEMA,
        },
        "constraints": [
            "authorization is owner-scoped and permanently bound to plan_digest",
            "CLI normal mode requires a ready existing voice; clone it first when needed",
        ],
    },
    "digital-human-oneclick-start": {
        "required": [
            "request_id", "consent_token", "plan_digest", "narration_mode",
            "portrait_upload_id", "allow_ai_materials", "customer_upload_ids",
        ],
        "properties": {
            "request_id": _DIGITAL_HUMAN_REQUEST_ID_SCHEMA,
            "consent_token": {"type": "string", "minLength": 32, "maxLength": 512},
            "plan_digest": _DIGITAL_HUMAN_PLAN_DIGEST_SCHEMA,
            "script": {"type": "string", "maxLength": 6000},
            "narration_mode": {"type": "string", "enum": ["text", "audio"]},
            "audio_upload_id": {"type": "string", "pattern": "^dha_[0-9a-f]{32}$"},
            "allow_ai_materials": {"type": "boolean"},
            "customer_upload_ids": _DIGITAL_HUMAN_MATERIAL_IDS_SCHEMA,
            "portrait_upload_id": {"type": "string", "pattern": "^img_[0-9a-f]{32}$"},
            "voice_key": {"type": "string", "maxLength": 180},
        },
        "constraints": [
            "quote first; confirmation must reuse identical input, quote_token, plan_digest and request_id",
            "replaying request_id returns the original run without duplicate child charges",
        ],
    },
    "digital-human-oneclick-status": {
        "required": ["run_id"], "properties": {
            "run_id": _DIGITAL_HUMAN_RUN_ID_SCHEMA,
        }, "constraints": ["only the run owner can read status"],
    },
    "digital-human-oneclick-recover": {
        "required": ["run_id", "request_id"], "properties": {
            "run_id": _DIGITAL_HUMAN_RUN_ID_SCHEMA,
            "request_id": _DIGITAL_HUMAN_REQUEST_ID_SCHEMA,
        }, "constraints": [
            "only refunded or zero-cost failed steps are resubmitted",
            "completed and in-flight children are never recreated",
        ],
    },
    "digital-human-oneclick-abandon": {
        "required": ["run_id", "request_id"], "properties": {
            "run_id": _DIGITAL_HUMAN_RUN_ID_SCHEMA,
            "request_id": _DIGITAL_HUMAN_REQUEST_ID_SCHEMA,
        }, "constraints": ["abandon stops future recovery but does not cancel already submitted billing"],
    },
    "digital-human-oneclick-history": {
        "required": [], "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "offset": {"type": "integer", "minimum": 0, "maximum": 2000},
        }, "constraints": ["only completed videos owned by the current account are returned"],
    },
})

_REQUEST_ID_SCHEMA = {
    "type": "string", "pattern": "^[A-Za-z0-9._:-]{8,128}$",
}
_SHORT_DRAMA_JOB_ID_SCHEMA = {
    "type": "string", "minLength": 1, "maxLength": 160,
}
_SHORT_DRAMA_PROVIDER_FIELDS = {
    "project_id": _ID_SCHEMA,
    "plan_id": _ID_SCHEMA,
    "shot_key": _ID_SCHEMA,
    "character_key": _ID_SCHEMA,
    "avatar_id": _ID_SCHEMA,
}
_DIRECTOR_WORKFLOW_ID_SCHEMA = {
    "type": "string", "pattern": "^dw_[0-9a-f]{32}$",
}
_DIRECTOR_STORYBOARD_SCHEMA = {
    "type": "array", "minItems": 1, "maxItems": 60,
    "items": {"type": "object"},
}
_MEDIA_SCHEMAS.update({
    "director-chat": {
        "required": ["prompt", "session_id", "page_revision", "page_context", "request_id"],
        "properties": {
            "prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
            "session_id": _ID_SCHEMA,
            "page_revision": _INT_ID_SCHEMA,
            "page_context": {"type": "object"},
            "history": {"type": "array", "maxItems": 12, "items": {"type": "object"}},
            "source_page": {"type": "string", "maxLength": 80},
            "request_id": _REQUEST_ID_SCHEMA,
        },
        "constraints": ["external AI call; confirm once and poll only the returned job_id"],
    },
    "director-produce": {
        "required": ["offer_id", "input", "expected_cost", "plan_digest", "quote_token"],
        "properties": {
            "offer_id": {"type": "string", "pattern": "^director-production-[A-Za-z0-9_-]{16,64}$"},
            "input": {"type": "object"},
            "expected_cost": {"type": "integer", "minimum": 0},
            "plan_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "quote_token": {"type": "string", "minLength": 20, "maxLength": 4096},
        },
        "constraints": ["all fields must come from the same director-chat production offer"],
    },
    "director-workflows": {
        "required": [],
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "offset": {"type": "integer", "minimum": 0, "maximum": 2000},
        },
    },
    "director-workflow-create": {
        "required": ["title", "request_id"],
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 120},
            "source_job_id": _INT_ID_SCHEMA,
            "storyboard": _DIRECTOR_STORYBOARD_SCHEMA,
            "request_id": _REQUEST_ID_SCHEMA,
        },
        "oneOf": [{"required": ["source_job_id"]}, {"required": ["storyboard"]}],
    },
    "director-workflow": {
        "required": ["workflow_id"], "properties": {"workflow_id": _DIRECTOR_WORKFLOW_ID_SCHEMA},
    },
    "director-storyboard-update": {
        "required": ["workflow_id", "revision", "storyboard"],
        "properties": {"workflow_id": _DIRECTOR_WORKFLOW_ID_SCHEMA, "revision": _INT_ID_SCHEMA, "storyboard": _DIRECTOR_STORYBOARD_SCHEMA},
        "constraints": ["revision must match the latest owner-scoped workflow"],
    },
    "director-storyboard-export": {
        "required": ["workflow_id"], "properties": {"workflow_id": _DIRECTOR_WORKFLOW_ID_SCHEMA},
    },
    "director-production-plan": {
        "required": ["workflow_id", "output_kind", "options"],
        "properties": {"workflow_id": _DIRECTOR_WORKFLOW_ID_SCHEMA,
                       "output_kind": {"type": "string", "enum": ["image", "video"]},
                       "options": {"type": "object"}},
        "constraints": ["freezes the current storyboard revision into one owner-scoped generation plan"],
    },
    "director-production-start": {
        "required": ["workflow_id", "plan_digest", "request_id"],
        "properties": {"workflow_id": _DIRECTOR_WORKFLOW_ID_SCHEMA,
                       "plan_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                       "request_id": _REQUEST_ID_SCHEMA},
        "constraints": ["quote and confirmation must reuse the same workflow revision, plan_digest and request_id"],
    },
    "director-production-status": {
        "required": ["workflow_id"], "properties": {"workflow_id": _DIRECTOR_WORKFLOW_ID_SCHEMA},
    },
    "director-production-recover": {
        "required": ["workflow_id", "plan_digest", "request_id"],
        "properties": {"workflow_id": _DIRECTOR_WORKFLOW_ID_SCHEMA,
                       "plan_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                       "request_id": _REQUEST_ID_SCHEMA},
        "constraints": ["replays only the original idempotent child submission"],
    },
    "director-remake-plan": {
        "required": ["workflow_id", "mode", "instruction", "options"],
        "properties": {"workflow_id": _DIRECTOR_WORKFLOW_ID_SCHEMA,
                       "mode": {"type": "string", "enum": ["cinematic", "grok", "micro"]},
                       "instruction": {"type": "string", "minLength": 1, "maxLength": 2000},
                       "options": {"type": "object"}},
    },
    "director-remake-start": {
        "required": ["workflow_id", "plan_digest", "request_id"],
        "properties": {"workflow_id": _DIRECTOR_WORKFLOW_ID_SCHEMA,
                       "plan_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                       "request_id": _REQUEST_ID_SCHEMA},
    },
    "director-remake-status": {
        "required": ["workflow_id"], "properties": {"workflow_id": _DIRECTOR_WORKFLOW_ID_SCHEMA},
    },
    "director-remake-recover": {
        "required": ["workflow_id", "plan_digest", "request_id"],
        "properties": {"workflow_id": _DIRECTOR_WORKFLOW_ID_SCHEMA,
                       "plan_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                       "request_id": _REQUEST_ID_SCHEMA},
    },
    "short-drama-advisor": {
        "required": ["user_message", "request_id"],
        "properties": {
            "messages": {"type": "array", "maxItems": 20, "items": {"type": "string", "maxLength": 600}},
            "understanding": {"type": "object"},
            "expected_field": {"type": "string", "enum": ["", "topic", "protagonist", "conflict", "emotion", "ending", "audience", "style"]},
            "field_states": {"type": "object"},
            "recommendation_context": {"type": "object"},
            "user_message": {"type": "string", "minLength": 1, "maxLength": 600},
            "request_id": _REQUEST_ID_SCHEMA,
        },
        "constraints": ["external AI call with platform-funded quota; confirm once per identical turn"],
    },
    "short-drama-character-reference-generate": {
        "required": ["project_id", "revision", "character_key"],
        "properties": {"project_id": _ID_SCHEMA, "revision": _INT_ID_SCHEMA, "character_key": _ID_SCHEMA},
        "constraints": ["server quote first; confirmation binds project revision and character profile snapshot"],
    },
    "short-drama-character-reference-confirm": {
        "required": ["project_id", "revision", "character_key", "reference_version"],
        "properties": {"project_id": _ID_SCHEMA, "revision": _INT_ID_SCHEMA, "character_key": _ID_SCHEMA, "reference_version": _INT_ID_SCHEMA},
    },
    "short-drama-preflight-plan": {
        "required": ["project_id", "conversation_revision", "request_id"],
        "properties": {"project_id": _ID_SCHEMA, "conversation_revision": _INT_ID_SCHEMA, "quality_route": {"type": "string", "enum": ["quick_draft", "quality_first"]}, "request_id": _REQUEST_ID_SCHEMA},
    },
    "short-drama-preflight-confirm": {
        "required": ["project_id", "plan_id", "plan_version", "accepted_issue_keys", "request_id"],
        "properties": {"project_id": _ID_SCHEMA, "plan_id": _ID_SCHEMA, "plan_version": _INT_ID_SCHEMA, "accepted_issue_keys": {"type": "array", "maxItems": 100, "items": _ID_SCHEMA}, "request_id": _REQUEST_ID_SCHEMA},
    },
    "short-drama-autodraft-preflight": {
        "required": ["project_id", "plan_id", "shot_key"],
        "properties": {**_SHORT_DRAMA_PROVIDER_FIELDS, "execution": {"type": "object"}},
    },
    "short-drama-autodraft-quote": {
        "required": ["project_id", "plan_id", "shot_key"],
        "properties": _SHORT_DRAMA_PROVIDER_FIELDS,
        "constraints": ["quote only; no points are deducted until start is confirmed"],
    },
    "short-drama-autodraft-start": {
        "required": ["project_id", "quote_token", "request_id"],
        "properties": {"project_id": _ID_SCHEMA, "quote_token": {"type": "string", "minLength": 1, "maxLength": 4096}, "request_id": _REQUEST_ID_SCHEMA},
        "constraints": ["confirm once; preserve request_id and poll only the returned job"],
    },
    "short-drama-autodraft-status": {
        "required": ["project_id", "job_id"],
        "properties": {"project_id": _ID_SCHEMA, "job_id": _SHORT_DRAMA_JOB_ID_SCHEMA},
    },
    "short-drama-delivery-quote": {
        "required": ["project_id", "version_id"],
        "properties": {"project_id": _ID_SCHEMA, "version_id": _ID_SCHEMA},
        "constraints": ["quote only; no points are deducted until start is confirmed"],
    },
    "short-drama-delivery-start": {
        "required": ["project_id", "quote_token", "request_id"],
        "properties": {"project_id": _ID_SCHEMA, "quote_token": {"type": "string", "minLength": 1, "maxLength": 4096}, "request_id": _REQUEST_ID_SCHEMA},
        "constraints": ["confirm once; preserve request_id and poll only the returned delivery job"],
    },
    "short-drama-delivery-status": {
        "required": ["project_id", "job_id"],
        "properties": {"project_id": _ID_SCHEMA, "job_id": _SHORT_DRAMA_JOB_ID_SCHEMA},
    },
    "short-drama-completion-readiness": {
        "required": ["project_id"], "properties": {"project_id": _ID_SCHEMA},
    },
    "short-drama-completion": {
        "required": ["project_id"], "properties": {"project_id": _ID_SCHEMA},
    },
    "short-drama-completion-confirm": {
        "required": ["project_id", "revision", "final_version_id", "asset_id", "delivery_hash", "acknowledged", "request_id"],
        "properties": {"project_id": _ID_SCHEMA, "revision": _INT_ID_SCHEMA, "final_version_id": _ID_SCHEMA, "asset_id": _ID_SCHEMA, "delivery_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"}, "acknowledged": {"type": "boolean", "const": True}, "request_id": _REQUEST_ID_SCHEMA},
        "constraints": ["irreversible owner-only completion; values must come from the latest readiness response"],
    },
})
_MEDIA_SCHEMAS.update({
    "director-scene-video-generate": {
        "required": ["scenes"],
        "properties": {
            "scenes": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"type": "object"}},
            **{key: item for key, item in _MEDIA_SCHEMAS["video-generate"]["properties"].items()
               if key != "prompt"},
        },
        "constraints": list(_MEDIA_SCHEMAS["video-generate"].get("constraints", ())),
    },
    "director-scene-talking-generate": {
        "required": list(_MEDIA_SCHEMAS["text-video-generate"]["required"]),
        "properties": dict(_MEDIA_SCHEMAS["text-video-generate"]["properties"]),
        "constraints": list(_MEDIA_SCHEMAS["text-video-generate"].get("constraints", ())),
    },
})

_FAMILIES = {
    "director-capability": "director", "director-script-generate": "director",
    "director-breakdown": "director", "director-breakdown-upload": "director",
    "director-scene-image-generate": "director", "director-scene-video-generate": "director",
    "director-scene-talking-generate": "director", "director-chat": "director",
    "director-produce": "director", "director-workflows": "director",
    "director-workflow-create": "director", "director-workflow": "director",
    "director-storyboard-update": "director", "director-storyboard-export": "director",
    "director-production-plan": "director", "director-production-start": "director",
    "director-production-status": "director", "director-production-recover": "director",
    "director-remake-plan": "director", "director-remake-start": "director",
    "director-remake-status": "director", "director-remake-recover": "director",
    "digital-human-oneclick-capability": "digital-human",
    "digital-human-oneclick-plan": "digital-human",
    "digital-human-oneclick-consent": "digital-human",
    "digital-human-oneclick-start": "digital-human",
    "digital-human-oneclick-status": "digital-human",
    "digital-human-oneclick-recover": "digital-human",
    "digital-human-oneclick-abandon": "digital-human",
    "digital-human-oneclick-history": "digital-human",
    "digital-human-oneclick-material-upload": "digital-human",
    "digital-human-oneclick-audio-upload": "digital-human",
    "image-upload": "image", "image-generate": "image", "audio-slots": "audio", "voices": "audio", "audio-generate": "audio",
    "voice-clone-create": "audio", "voice-clone-status": "audio",
    "video-upload": "video", "video-avatars": "video", "video-generate": "video", "video-lipsync": "video", "digital-ip-text-generate": "video",
    "video-avatar-create": "video",
    "digital-ip-batch-generate": "video", "digital-ip-audio-generate": "video", "cinematic-open-generate": "video",
    "cinematic-motion-generate": "video", "tryon-fast-generate": "video", "tryon-classic-generate": "video",
    "video-compose-projects": "video", "video-compose-project": "video", "video-compose-create": "video",
    "video-compose-analyze": "video", "video-compose-review": "video", "video-compose-render": "video",
    "text-video-capability": "video", "text-video-templates": "video", "text-video-styles": "video", "text-video-voices": "video",
    "text-video-generate": "video",
    "matrix-template-capability": "video", "matrix-template-templates": "video",
    "matrix-template-generate": "video", "matrix-template-batch-generate": "video",
    "text-video-avatar-import": "video", "text-video-plan": "video",
    "canvas-list": "canvas", "canvas-get": "canvas", "canvas-create": "canvas", "canvas-agent-plan": "canvas",
    "canvas-ops": "canvas", "digital-presenter-capability": "canvas", "digital-presenter-project": "canvas",
    "digital-presenter-create": "canvas", "digital-presenter-update": "canvas",
    "short-drama-projects": "short-drama", "short-drama-project": "short-drama",
    "short-drama-conversation": "short-drama", "short-drama-preflight": "short-drama",
    "short-drama-create": "short-drama", "short-drama-delete": "short-drama",
    "short-drama-advisor": "short-drama",
    "short-drama-character-reference-generate": "short-drama",
    "short-drama-character-reference-confirm": "short-drama",
    "short-drama-preflight-plan": "short-drama", "short-drama-preflight-confirm": "short-drama",
    "short-drama-autodraft-preflight": "short-drama", "short-drama-autodraft-quote": "short-drama",
    "short-drama-autodraft-start": "short-drama", "short-drama-autodraft-status": "short-drama",
    "short-drama-delivery-quote": "short-drama", "short-drama-delivery-start": "short-drama",
    "short-drama-delivery-status": "short-drama", "short-drama-completion-readiness": "short-drama",
    "short-drama-completion": "short-drama", "short-drama-completion-confirm": "short-drama",
}
_ACTION_FEATURE_GATES = {
    "director-script-generate": ("copy",), "director-breakdown": ("breakdown",),
    "director-scene-image-generate": ("image",),
    "director-scene-video-generate": ("video",),
    "director-scene-talking-generate": ("script_to_video",),
    "director-chat": ("director_agent",), "director-produce": ("director_agent",),
    "audio-generate": ("audio",), "voice-clone-create": ("audio",), "voice-clone-status": ("audio",),
    "video-avatar-create": ("avatar",),
    "canvas-agent-plan": ("canvas_agent",),
    "video-lipsync": ("video",), "digital-ip-text-generate": ("video",), "digital-ip-batch-generate": ("video",),
    "digital-ip-audio-generate": ("video",), "cinematic-open-generate": ("cinematic",),
    "cinematic-motion-generate": ("cinematic",), "tryon-fast-generate": ("tryon",),
    "tryon-classic-generate": ("tryon",), "digital-presenter-capability": ("digital_presenter",),
    "digital-presenter-project": ("digital_presenter",), "digital-presenter-create": ("digital_presenter",),
    "digital-presenter-update": ("digital_presenter",), "text-video-generate": ("script_to_video",),
    "matrix-template-generate": ("matrix_template_video",),
    "matrix-template-batch-generate": ("matrix_template_video",),
    "text-video-avatar-import": ("script_to_video",), "text-video-plan": ("script_to_video",),
}
_OPTION_FEATURE_GATES = {
    ("image-generate", "provider"): {"openai": ("image",), "seedream": ("image",), "xiaole": ("image", "image_xiaole"), "banana": ("image", "banana")},
    ("video-generate", "channel"): {"grok": ("grok_video",), "micro": ("seedance_video",), "omni": ("omni_video",), "minimax": ("minimax_h3_video",), "sora": ("sora_video",)},
}
CATALOG_FEATURE_FLAGS = tuple(sorted({flag for flags in (*_ACTION_FEATURE_GATES.values(), *(
    gates for options in _OPTION_FEATURE_GATES.values() for gates in options.values())) for flag in flags}))

_GENERATION_ACTIONS = frozenset({
    "collect-content", "collect-video", "collect-transcript", "collect-search", "leads-generate",
    "director-script-generate", "director-breakdown", "director-scene-image-generate",
    "director-scene-video-generate", "director-scene-talking-generate",
    "director-production-start", "director-remake-start",
    "canvas-agent-plan", "image-generate", "video-generate", "video-lipsync", "audio-generate",
    "digital-ip-text-generate", "digital-ip-batch-generate", "digital-ip-audio-generate",
    "cinematic-open-generate", "cinematic-motion-generate", "tryon-fast-generate", "tryon-classic-generate",
    "text-video-generate", "video-avatar-create",
    "matrix-template-generate", "matrix-template-batch-generate",
    "digital-human-oneclick-start",
    "short-drama-character-reference-generate",
})


_WEB_PARITY_INTEGER_FIELDS = frozenset({
    "amount", "revision", "expected_revision", "expected_quote_expires_at", "notification_id",
    "limit", "offset", "level", "shot_count", "target_duration", "project_revision",
    "graph_revision", "point_budget", "asset_job_id", "digital_human_item_index",
})
_WEB_PARITY_BOOLEAN_FIELDS = frozenset({"consent", "locked", "digital_human_allow_ai_materials"})
_WEB_PARITY_ARRAY_FIELDS = frozenset({
    "assets", "history", "candidate_ids", "scenes", "planning_messages",
    "character_contract", "characters", "shots", "shot_keys", "digital_human_customer_upload_ids",
})
_WEB_PARITY_OBJECT_FIELDS = frozenset({
    "payload", "state", "patch", "profile", "scene", "structure", "changes", "issue",
    "project", "confirmed_contract", "core_story", "script", "checklist", "source_hashes",
})


def _web_parity_schema_field(action, name):
    if name in _WEB_PARITY_INTEGER_FIELDS:
        return {"type": "integer", "minimum": 1 if name == "notification_id" else 0, "maximum": 2**63 - 1}
    if name in _WEB_PARITY_BOOLEAN_FIELDS:
        return {"type": "boolean"}
    if name == "assets":
        return {"type": "array", "minItems": 1, "maxItems": 120, "items": {
            "type": "object", "additionalProperties": False, "required": ["kind", "id"],
            "properties": {"kind": {"type": "string", "enum": ["image", "audio", "video", "avatar"]},
                           "id": {"type": "integer", "minimum": 1, "maximum": 2**63 - 1}},
        }}
    if name in _WEB_PARITY_ARRAY_FIELDS:
        return {"type": "array", "maxItems": 120, "items": {"type": "string"}} if name in {"planning_messages", "shot_keys"} else {"type": "array", "maxItems": 120}
    if name in _WEB_PARITY_OBJECT_FIELDS:
        return {"type": "object"}
    field = {"type": "string", "maxLength": 8000}
    if name.endswith("_id") or name in {"message", "title", "account_id", "username", "url", "product_type", "order_type", "action"}:
        field["minLength"] = 1
    if name == "request_id":
        field.update({"minLength": 8, "maxLength": 128, "pattern": "^[A-Za-z0-9._:-]{8,128}$"})
    if name == "action":
        field["enum"] = (["accept", "reject"] if action == "friend-request-respond" else
                         ["delete", "copy", "insert_before", "insert_after", "smart_insert", "move_up", "move_down"])
    if name == "role":
        field["enum"] = ["editor", "viewer"]
    if name == "source" and action in {"short-drama-scene-reference", "short-drama-character-reference-select"}:
        field["enum"] = ["asset", "upload"]
    if name == "ratio":
        field["enum"] = ["9:16", "16:9"]
    if name == "import_mode":
        field["enum"] = ["faithful", "optimize"]
    if name == "content_type":
        field["enum"] = ["live_action"]
    if name == "source_requirement":
        field["enum"] = ["", "complete_story"]
    if name == "mode" and action == "short-drama-refinement-media":
        field["enum"] = ["voice_timeline", "provider_audio", "silent"]
    if name == "digital_human_pipeline":
        field["const"] = "digital_human_material_v3"
    if name == "digital_human_stage":
        field["const"] = "material_resolve"
    if name == "digital_human_narration_mode":
        field["enum"] = ["text", "audio"]
    return field


_WEB_ACTION_INPUTS = {
    action: {
        "required": list(required),
        "properties": {field: _web_parity_schema_field(action, field) for field in fields},
        "constraints": [
            "fixed first-party route; unknown input fields are rejected",
            "owner scope is enforced by the target API",
            *(["use the same request_id on a retry"] if idempotent else []),
        ],
    }
    for action, (_, _, _, fields, required, _, idempotent) in WEB_PARITY_ACTIONS.items()
}
_MEDIA_SCHEMAS.update(_WEB_ACTION_INPUTS)


def _catalog_type(field):
    if field in {"id", "avatar_id", "job_id", "limit", "offset", "page", "page_size", "days", "count", "duration", "seconds", "expected_version", "expected_revision", "revision", "source_asset_id", "target_duration", "pitch", "volume"}:
        return "integer"
    if field in {"favorite", "subtitle", "enhance_prompt", "generate_audio"}:
        return "boolean"
    if field.endswith("ids") or field in {"ops", "nodes", "edges", "history", "avatars", "lead_ids", "tags"}:
        return "array"
    if field in {"decisions", "channels_targets"}:
        return "object"
    if field == "speed":
        return "number"
    return "string"


def _catalog_route(action):
    if action.startswith("director-"):
        return "/workbench/script"
    if action.startswith("digital-human-oneclick-"):
        return "/workbench/digital-human-oneclick.html"
    if action.startswith("canvas-") or action.startswith("digital-presenter-"):
        return "/workbench/canvas"
    if action.startswith("image-"):
        return "/workbench/banana"
    if action.startswith("audio-") or action == "voices":
        return "/workbench/audio"
    if action.startswith("text-video-"):
        return "/workbench/text-video"
    if action.startswith("matrix-template-"):
        return "/workbench/matrix-template"
    if action.startswith("video-compose-"):
        return "/workbench/one-click-video"
    if action.startswith("digital-ip-"):
        return "/workbench/digital-ip"
    if action.startswith(("video-", "cinematic-", "tryon-")):
        return "/workbench/video"
    if action.startswith("ip12-"):
        return "/workbench/ip12/"
    if action.startswith("short-drama-"):
        return "/workbench/short-drama"
    return ""


def _catalog_entry(action, fields):
    generation = action in _GENERATION_ACTIONS
    external_effect = generation or action in CONFIRMATION_ACTIONS
    details = _MEDIA_SCHEMAS.get(action, {})
    schema = {
        "type": "object", "additionalProperties": False,
        "required": list(details.get("required", ())),
        "properties": details.get("properties") or {field: {"type": _catalog_type(field)} for field in fields},
    }
    for keyword in ("allOf", "anyOf", "oneOf", "x-hq-channel-rules"):
        if keyword in details:
            schema[keyword] = details[keyword]
    result_type = "quote" if generation else ("account" if action == "account" else "json")
    return {
        "action": action,
        "purpose": _ACTION_PURPOSES.get(action, "执行黄雀已登记能力：" + action),
        "input_schema": schema,
        "constraints": list(details.get("constraints", ())),
        "billing": "quote_then_confirm" if generation else "free",
        "external_effect": external_effect,
        "confirmation_required": generation or action in CONFIRMATION_ACTIONS,
        "risk": "production" if generation else ("write" if external_effect else "read"),
        "result_type": result_type, "result": {"kind": result_type},
        "ui_route": _catalog_route(action),
        "transport": {"kind": "action", "supports": ["internal_action", "controlled_shell"]},
        "availability": {"status": "available", "feature_flags": [], "disabled_feature_flags": []},
    }


def _upload_catalog_entry(action, family, max_bytes, mime_types, max_files):
    label = {"image": "图片", "video": "视频", "audio": "音频"}[family]
    return {
        "action": action, "family": family, "purpose": "上传本人生成所需的临时参考" + label,
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["file"], "properties": {
            "file": {"type": "file", "path": "absolute", "maxBytes": max_bytes, "mimeTypes": mime_types},
        }},
        "constraints": ["requires explicit confirmation", "uploads are private to the current account"],
        "billing": "free", "external_effect": True, "confirmation_required": True, "risk": "write",
        "result_type": "upload", "result": {"kind": "upload_id"}, "ui_route": _catalog_route(action),
        "transport": {"kind": "dedicated_upload", "supports": ["dedicated_upload"], "account_active_max_files": max_files},
        "availability": {"status": "available", "feature_flags": [], "disabled_feature_flags": []},
    }


def _director_breakdown_upload_catalog_entry():
    return {
        "action": "director-breakdown-upload", "family": "director",
        "purpose": "上传本人本地图片或视频并创建提示词反推任务",
        "input_schema": {"type": "object", "additionalProperties": False,
                         "required": ["file"], "properties": {"file": {
                             "type": "file", "path": "absolute",
                             "maxBytes": 200 * 1024 * 1024,
                             "mimeTypes": ["image/jpeg", "image/png", "image/webp",
                                           "video/mp4", "video/quicktime", "video/webm"],
                         }}},
        "constraints": [
            "quote first with the account, media type and file SHA-256, then confirm the identical upload",
            "X-HQ-Expected-Cost must equal cost from that same quote response",
            "requires a stable Idempotency-Key; retrying the same file replays the original job",
            "images are limited to 20 MiB; videos are limited to 200 MiB and 120 seconds",
            "the upload is used only for the current account's reverse-prompt job",
        ],
        "billing": "quote_then_confirm", "external_effect": True,
        "confirmation_required": True, "risk": "production",
        "result_type": "job", "result": {"kind": "job_id"},
        "ui_route": "/workbench/script",
        "transport": {"kind": "dedicated_upload", "supports": ["dedicated_upload"],
                      "quote_path": "/api/auth/cli/director-breakdown-quote",
                      "quote_token_header": "X-HQ-Quote-Token",
                      "expected_cost_header": "X-HQ-Expected-Cost",
                      "idempotency_header": "Idempotency-Key",
                      "idempotency_key_pattern": r"^[A-Za-z0-9._:-]{8,128}$",
                      "account_active_max_files": 2},
        "availability": {"status": "available", "feature_flags": ["breakdown"],
                         "disabled_feature_flags": []},
    }


def _download_catalog_entry():
    return {
        "action": "dl", "family": "collect", "purpose": "下载黄雀已返回的无水印视频或图片",
        "input_schema": {
            "type": "object", "additionalProperties": False,
            "required": ["url", "output_file"],
            "properties": {
                "url": {"type": "string", "minLength": 8, "maxLength": 4096},
                "name": {"type": "string", "minLength": 1, "maxLength": 40},
                "decode_key": {"type": "string", "maxLength": 4096},
                "output_file": {"type": "file", "path": "absolute"},
            },
        },
        "constraints": [
            "the server accepts only its fixed media CDN allowlist",
            "the client refuses redirects, symlinked parents and existing output files",
            "decode_key is sent in a header instead of the URL",
        ],
        "billing": "free", "external_effect": False,
        "confirmation_required": False, "risk": "read",
        "result_type": "file", "result": {"kind": "local_file"},
        "ui_route": "/workbench/assets",
        "transport": {"kind": "download", "supports": ["fixed_download"]},
        "availability": {"status": "available", "feature_flags": [], "disabled_feature_flags": []},
    }


def _asset_batch_download_catalog_entry():
    return {
        "action": "asset-batch-download", "family": "assets",
        "purpose": "把本人选择的资产打包下载到一个新的本地 ZIP 文件",
        "input_schema": {"type": "object", "additionalProperties": False,
                         "required": ["assets", "output_file"], "properties": {
                             "assets": _web_parity_schema_field("asset-batch-download", "assets"),
                             "output_file": {"type": "file", "path": "absolute"},
                         }},
        "constraints": ["fixed POST download route", "existing local files are never overwritten"],
        "billing": "free", "external_effect": False, "confirmation_required": False,
        "risk": "read", "result_type": "file", "result": {"kind": "local_file"},
        "ui_route": "/workbench/assets", "transport": {"kind": "download", "supports": ["fixed_download"]},
        "availability": {"status": "available", "feature_flags": [], "disabled_feature_flags": []},
    }


def _account_media_upload_catalog_entry(action, purpose, max_bytes, mime_types, ui_route):
    return {
        "action": action, "family": "assets", "purpose": purpose,
        "input_schema": {"type": "object", "additionalProperties": False,
                         "required": ["file"], "properties": {"file": {
                             "type": "file", "path": "absolute", "maxBytes": max_bytes,
                             "mimeTypes": mime_types,
                         }}},
        "constraints": ["requires explicit confirmation", "the local path is never sent to the server"],
        "billing": "free", "external_effect": True, "confirmation_required": True,
        "risk": "write", "result_type": "upload", "result": {"kind": "account_asset"},
        "ui_route": ui_route,
        "transport": {"kind": "dedicated_upload", "supports": ["dedicated_upload"]},
        "availability": {"status": "available", "feature_flags": [], "disabled_feature_flags": []},
    }


def _creator_pdf_download_catalog_entry():
    return {
        "action": "creator-agent-background-pdf", "family": "creator-agent",
        "purpose": "下载本人 Creator Agent 项目的画像背景 PDF",
        "input_schema": {"type": "object", "additionalProperties": False,
                         "required": ["project_id", "output_file"], "properties": {
                             "project_id": {"type": "string", "pattern": "^[0-9a-f]{12}$"},
                             "output_file": {"type": "file", "path": "absolute"},
                         }},
        "constraints": ["fixed owner-scoped PDF route", "existing local files are never overwritten"],
        "billing": "free", "external_effect": False, "confirmation_required": False,
        "risk": "read", "result_type": "file", "result": {"kind": "local_file"},
        "ui_route": "/workbench/creator-agent",
        "transport": {"kind": "download", "supports": ["fixed_download"]},
        "availability": {"status": "available", "feature_flags": ["creator_agent_v1"], "disabled_feature_flags": []},
    }


ACTION_CATALOG = tuple(_catalog_entry(action, fields) for action, fields in _ACTION_INPUTS.items()) + (
    _upload_catalog_entry("image-upload", "image", 10 * 1024 * 1024,
                          ["image/jpeg", "image/png", "image/webp"], 20),
    _upload_catalog_entry("video-upload", "video", 32 * 1024 * 1024,
                          ["video/mp4", "video/quicktime", "video/webm"], 6),
    _upload_catalog_entry("audio-upload", "audio", 10 * 1024 * 1024,
                          ["audio/mpeg", "audio/wav", "audio/x-wav", "audio/mp4", "audio/x-m4a", "audio/aac", "audio/ogg"], 20),
    _upload_catalog_entry("digital-human-oneclick-material-upload", "image", 10 * 1024 * 1024,
                          ["image/jpeg", "image/png", "image/webp"], 12),
    _upload_catalog_entry("digital-human-oneclick-audio-upload", "audio", 30 * 1024 * 1024,
                          ["audio/mpeg", "audio/wav", "audio/x-wav", "audio/mp4", "audio/x-m4a", "audio/aac"], 1),
    _director_breakdown_upload_catalog_entry(),
    _download_catalog_entry(),
    _asset_batch_download_catalog_entry(),
    _account_media_upload_catalog_entry(
        "profile-avatar-upload", "上传并更新当前账号头像", 4 * 1024 * 1024,
        ["image/jpeg", "image/png", "image/webp"], "/workbench/settings",
    ),
    _account_media_upload_catalog_entry(
        "video-import", "导入本人 H3 MP4 成片到视频资产库", 100 * 1024 * 1024,
        ["video/mp4"], "/workbench/video",
    ),
    _creator_pdf_download_catalog_entry(),
)
for _catalog_item in ACTION_CATALOG:
    if _catalog_item["action"] in _FAMILIES:
        _catalog_item["family"] = _FAMILIES[_catalog_item["action"]]
ACTION_CATALOG_MAP = {item["action"]: item for item in ACTION_CATALOG if item["transport"]["kind"] == "action"}
ACTION_CATALOG_VERSION = "hq-action-catalog-v8"


def action_catalog(feature_states=None):
    """Return a copy-safe, provider-secret-free catalog for first-party callers."""
    feature_states = feature_states or {}
    actions = json.loads(json.dumps(ACTION_CATALOG, ensure_ascii=False))
    for item in actions:
        action = item["action"]
        required = _ACTION_FEATURE_GATES.get(action, ())
        disabled = [flag for flag in required if not feature_states.get(flag, True)]
        availability = item["availability"]
        availability["feature_flags"] = list(required)
        availability["disabled_feature_flags"] = disabled
        if disabled:
            availability["status"] = "disabled"
        for (option_action, field), options in _OPTION_FEATURE_GATES.items():
            if action != option_action:
                continue
            available = [option for option, flags in options.items() if all(feature_states.get(flag, True) for flag in flags)]
            unavailable = {option: [flag for flag in flags if not feature_states.get(flag, True)]
                           for option, flags in options.items() if option not in available}
            item["input_schema"]["properties"][field]["enum"] = available
            availability["available_%s" % field] = available
            availability["disabled_%s" % field] = unavailable
            if not available:
                availability["status"] = "disabled"
    return {
        "version": ACTION_CATALOG_VERSION,
        "actions": actions,
    }

_START_HITS = {}
_START_HITS_LOCK = threading.Lock()
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_UPLOAD_ID_RE = re.compile(r"^img_[0-9a-f]{32}$")
_VIDEO_UPLOAD_ID_RE = re.compile(r"^vid_[0-9a-f]{32}$")
_AUDIO_UPLOAD_ID_RE = re.compile(r"^aud_[0-9a-f]{32}$")
_CANVAS_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_CANVAS_OP_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_CANVAS_PROJECT_RE = re.compile(r"^(local|collab):[A-Za-z0-9_-]{1,120}$")
_CANVAS_OP_ID_RE = re.compile(r"^hqcli-[A-Za-z0-9_-]{11,122}$")
_VIDEO_COMPOSE_PROJECT_RE = re.compile(r"^compose_[0-9a-f]{32}$")
_VIDEO_COMPOSE_CANDIDATE_RE = re.compile(r"^candidate_[0-9a-f]{16}$")
_DIGITAL_PRESENTER_PROJECT_RE = re.compile(r"^dp_[0-9a-f]{32}$")
_LEAD_ID_RE = re.compile(r"^[0-9a-f]{16,40}$")
_TALKING_PLAN_ID_RE = re.compile(r"^talking_plan_[0-9a-f]{32}$")
_TALKING_AVATAR_ID_RE = re.compile(r"^local_avatar_[0-9a-f]{32}$")
_TALKING_SCENE_ID_RE = re.compile(r"^scene_[0-9]{2}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_DIGITAL_HUMAN_RUN_RE = re.compile(r"^dh-run-[A-Za-z0-9._:-]{1,128}$")
_DIRECTOR_WORKFLOW_RE = re.compile(r"^dw_[0-9a-f]{32}$")
_CANVAS_BASE64_RE = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{512,}={0,2}(?![A-Za-z0-9+/_=-])")
IMAGE_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
IMAGE_UPLOAD_SLOTS = threading.BoundedSemaphore(2)
VIDEO_UPLOAD_MAX_BYTES = 32 * 1024 * 1024
VIDEO_UPLOAD_SLOTS = threading.BoundedSemaphore(2)
AUDIO_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
AUDIO_UPLOAD_SLOTS = threading.BoundedSemaphore(2)
DIRECTOR_BREAKDOWN_IMAGE_MAX_BYTES = 20 * 1024 * 1024
DIRECTOR_BREAKDOWN_VIDEO_MAX_BYTES = 200 * 1024 * 1024
DIRECTOR_BREAKDOWN_UPLOAD_SLOTS = threading.BoundedSemaphore(2)
_TASK_KINDS = {
    "", "image", "audio", "video", "xiaole_video", "copy", "collect", "collect_search", "leads",
    "tryon", "cinematic", "avatar", "breakdown", "script_to_video", "sora_video",
    "matrix_template_video",
}


class CLIAPIError(Exception):
    def __init__(self, status, detail, code="invalid_request"):
        super().__init__(detail)
        self.status = int(status)
        self.detail = str(detail)
        self.code = str(code)


def init_schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS cli_device_grants(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_code_hash TEXT NOT NULL UNIQUE,
        user_code_hash TEXT NOT NULL UNIQUE,
        client_name TEXT NOT NULL,
        requested_scopes_json TEXT NOT NULL,
        approved_scopes_json TEXT,
        username TEXT,
        status TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        approved_at INTEGER,
        last_poll_at INTEGER NOT NULL DEFAULT 0,
        token_hash TEXT UNIQUE,
        token_expires_at INTEGER,
        revoked_at INTEGER
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_cli_grants_user ON cli_device_grants(username, token_expires_at)")
    connection.execute("""CREATE TABLE IF NOT EXISTS cli_action_requests(
        username TEXT NOT NULL,
        action TEXT NOT NULL,
        request_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        http_status INTEGER,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY(username, action, request_id)
    )""")
    connection.execute("""CREATE INDEX IF NOT EXISTS idx_cli_action_active
        ON cli_action_requests(username, action, project_id, status, updated_at)""")


def _hash(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def begin_action_request(db_factory, username, action, request_id, project_id, request_hash, now=None):
    """Claim one persistent CLI action or describe the existing claim."""
    now = int(time.time() if now is None else now)
    connection = db_factory()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM cli_action_requests WHERE updated_at<?", (now - ACTION_REQUEST_TTL,))
        connection.execute(
            "UPDATE cli_action_requests SET status='uncertain',updated_at=? "
            "WHERE status='in_progress' AND updated_at<?",
            (now, now - ACTION_INFLIGHT_TTL),
        )
        row = connection.execute(
            "SELECT request_hash,status,http_status FROM cli_action_requests "
            "WHERE username=? AND action=? AND request_id=?",
            (username, action, request_id),
        ).fetchone()
        if row:
            connection.commit()
            if row["request_hash"] != request_hash:
                return "conflict", row["http_status"]
            return row["status"], row["http_status"]
        recent = connection.execute(
            "SELECT COUNT(*) FROM cli_action_requests WHERE username=? AND action=? AND created_at>=?",
            (username, action, now - 60),
        ).fetchone()[0]
        if int(recent) >= CLI_CHAT_REQUESTS_PER_MINUTE:
            connection.commit()
            return "rate_limited", None
        active = connection.execute(
            "SELECT status FROM cli_action_requests WHERE username=? AND action=? AND project_id=? "
            "AND (status='in_progress' OR (status='uncertain' AND updated_at>=?)) "
            "ORDER BY updated_at DESC LIMIT 1",
            (username, action, project_id, now - ACTION_INFLIGHT_TTL),
        ).fetchone()
        if active:
            connection.commit()
            return ("uncertain" if active["status"] == "uncertain" else "busy"), None
        connection.execute(
            "INSERT INTO cli_action_requests(username,action,request_id,project_id,request_hash,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'in_progress',?,?)",
            (username, action, request_id, project_id, request_hash, now, now),
        )
        connection.commit()
        return "new", None
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def finish_action_request(db_factory, username, action, request_id, http_status=None, uncertain=False, now=None):
    now = int(time.time() if now is None else now)
    connection = db_factory()
    try:
        connection.execute(
            "UPDATE cli_action_requests SET status=?,http_status=?,updated_at=? "
            "WHERE username=? AND action=? AND request_id=? AND status='in_progress'",
            ("uncertain" if uncertain else "completed", http_status, now, username, action, request_id),
        )
        connection.commit()
    finally:
        connection.close()


def _strict_object(value, allowed, required=()):
    if not isinstance(value, dict):
        raise CLIAPIError(400, "请求体必须是 JSON 对象")
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise CLIAPIError(400, "不支持的参数：" + unknown[0])
    missing = [key for key in required if key not in value]
    if missing:
        raise CLIAPIError(400, "缺少参数：" + missing[0])
    return value


def _string(value, field, minimum=0, maximum=2000):
    if not isinstance(value, str):
        raise CLIAPIError(400, field + " 必须是字符串")
    value = value.strip()
    if len(value) < minimum or len(value) > maximum or any(ord(ch) < 32 for ch in value):
        raise CLIAPIError(400, field + " 长度或内容不合法")
    return value


def _integer(value, field, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise CLIAPIError(400, "%s 必须是 %d-%d 的整数" % (field, minimum, maximum))
    return value


def _enum(value, field, choices):
    value = _string(value, field, 1, 80)
    if value not in choices:
        raise CLIAPIError(400, field + " 仅支持：" + "、".join(choices))
    return value


def _identifier(value, field):
    value = _string(value, field, 1, 160)
    if not _ID_RE.fullmatch(value):
        raise CLIAPIError(400, field + " 格式不合法")
    return value


def _upload_id(value, field):
    value = _string(value, field, 1, 64).lower()
    if not _UPLOAD_ID_RE.fullmatch(value):
        raise CLIAPIError(400, field + " 格式不合法")
    return value


def _video_upload_id(value, field):
    value = _string(value, field, 1, 64).lower()
    if not _VIDEO_UPLOAD_ID_RE.fullmatch(value):
        raise CLIAPIError(400, field + " 格式不合法")
    return value


def _audio_upload_id(value, field):
    value = _string(value, field, 1, 64).lower()
    if not _AUDIO_UPLOAD_ID_RE.fullmatch(value):
        raise CLIAPIError(400, field + " 格式不合法")
    return value


def _number(value, field, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CLIAPIError(400, field + " 必须是有限数字")
    if not minimum <= value <= maximum:
        raise CLIAPIError(400, field + " 超出允许范围")
    return value


def _canvas_node_id(value, field="节点标识"):
    value = _string(value, field, 1, 128)
    if not _CANVAS_NODE_ID_RE.fullmatch(value):
        raise CLIAPIError(400, field + " 格式不合法")
    return value


def _canvas_op_node_id(value, field="节点标识"):
    value = _string(value, field, 1, 64)
    if not _CANVAS_OP_NODE_ID_RE.fullmatch(value):
        raise CLIAPIError(400, field + " 格式不合法")
    return value


def _canvas_agent_payload(value):
    _strict_object(value, {
        "prompt", "project_id", "snapshot_digest", "scope", "nodes", "edges",
        "selected_node_ids", "history", "page_context", "ip12_context",
    }, ("prompt", "project_id", "snapshot_digest", "scope", "nodes", "edges", "selected_node_ids"))
    project_id = _string(value["project_id"], "project_id", 1, 128)
    scope = _enum(value["scope"], "scope", ("local", "collab"))
    if not _CANVAS_PROJECT_RE.fullmatch(project_id) or not project_id.startswith(scope + ":"):
        raise CLIAPIError(400, "project_id 与 scope 不匹配")
    digest = _string(value["snapshot_digest"], "snapshot_digest", 8, 32).lower()
    if not re.fullmatch(r"[a-f0-9]{8,32}", digest):
        raise CLIAPIError(400, "snapshot_digest 格式不合法")
    raw_nodes = value["nodes"]
    if not isinstance(raw_nodes, list) or len(raw_nodes) > 60:
        raise CLIAPIError(400, "nodes 必须是最多 60 项的数组")
    nodes, node_ids, total_content = [], set(), 0
    for raw in raw_nodes:
        _strict_object(raw, {"id", "type", "title", "content", "selected"},
                       ("id", "type", "title", "content", "selected"))
        node_id = _canvas_node_id(raw["id"])
        if node_id in node_ids:
            raise CLIAPIError(400, "nodes 包含重复节点")
        if not isinstance(raw["selected"], bool):
            raise CLIAPIError(400, "selected 必须是布尔值")
        node = {
            "id": node_id,
            "type": _enum(raw["type"], "节点类型", ("text", "image", "reverse", "gen", "video", "shortDrama")),
            "title": _string(raw["title"], "节点标题", 0, 120),
            "content": _string(raw["content"], "节点内容", 0, 5000),
            "selected": raw["selected"],
        }
        total_content += len(node["title"]) + len(node["content"])
        node_ids.add(node_id)
        nodes.append(node)
    if total_content > 30000:
        raise CLIAPIError(400, "画布上下文文本超过限制")
    selected = value["selected_node_ids"]
    if not isinstance(selected, list) or len(selected) > 30:
        raise CLIAPIError(400, "selected_node_ids 必须是最多 30 项的数组")
    selected = [_canvas_node_id(item, "选中节点标识") for item in selected]
    if len(selected) != len(set(selected)) or any(item not in node_ids for item in selected):
        raise CLIAPIError(400, "selected_node_ids 引用了无效节点")
    raw_edges = value["edges"]
    if not isinstance(raw_edges, list) or len(raw_edges) > 120:
        raise CLIAPIError(400, "edges 必须是最多 120 项的数组")
    edges = []
    for raw in raw_edges:
        _strict_object(raw, {"from_node_id", "to_node_id"}, ("from_node_id", "to_node_id"))
        source = _canvas_node_id(raw["from_node_id"], "连线起点")
        target = _canvas_node_id(raw["to_node_id"], "连线终点")
        if source == target or source not in node_ids or target not in node_ids:
            raise CLIAPIError(400, "edges 引用了无效节点")
        edges.append({"from_node_id": source, "to_node_id": target})
    raw_history = value.get("history", [])
    if not isinstance(raw_history, list) or len(raw_history) > 10:
        raise CLIAPIError(400, "history 必须是最多 10 项的数组")
    history = []
    for raw in raw_history:
        _strict_object(raw, {"role", "content"}, ("role", "content"))
        role = _enum(raw["role"], "历史角色", ("user", "assistant"))
        content = _string(raw["content"], "历史消息", 0, 2000)
        if content:
            history.append({"role": role, "content": content})
    page_context = value.get("page_context")
    if page_context is not None:
        _strict_object(page_context, {"page", "path", "title", "can_edit", "selected_count"},
                       ("page", "path", "title", "can_edit", "selected_count"))
        if page_context["page"] != "canvas" or page_context["path"] not in {
                "/workbench/canvas", "/workbench/canvas.html"}:
            raise CLIAPIError(400, "page_context 不属于黄雀画布")
        if not isinstance(page_context["can_edit"], bool):
            raise CLIAPIError(400, "page_context.can_edit 必须是布尔值")
        page_context = {
            "page": "canvas", "path": page_context["path"],
            "title": _string(page_context["title"], "page_context.title", 0, 120),
            "can_edit": page_context["can_edit"],
            "selected_count": _integer(page_context["selected_count"], "page_context.selected_count", 0, 30),
        }
    ip12_context = value.get("ip12_context")
    if ip12_context is not None:
        _strict_object(ip12_context, {"project_id", "title", "status", "foundation_status", "facts"},
                       ("project_id", "title", "status", "foundation_status", "facts"))
        raw_facts = ip12_context["facts"]
        if not isinstance(raw_facts, list) or len(raw_facts) > 20:
            raise CLIAPIError(400, "ip12_context.facts 必须是最多 20 项的数组")
        facts = []
        for raw in raw_facts:
            _strict_object(raw, {"label", "value"}, ("label", "value"))
            facts.append({
                "label": _string(raw["label"], "ip12_context.facts.label", 1, 80),
                "value": _string(raw["value"], "ip12_context.facts.value", 1, 800),
            })
        ip12_context = {
            "project_id": _identifier(ip12_context["project_id"], "ip12_context.project_id"),
            "title": _string(ip12_context["title"], "ip12_context.title", 0, 120),
            "status": _enum(ip12_context["status"], "ip12_context.status", ("draft", "candidate_ready", "confirmed")),
            "foundation_status": _enum(ip12_context["foundation_status"], "ip12_context.foundation_status",
                                       ("missing", "pending_confirmation", "confirmed", "stale", "legacy")),
            "facts": facts,
        }
    payload = {
        "prompt": _string(value["prompt"], "prompt", 1, 2000), "project_id": project_id,
        "snapshot_digest": digest, "scope": scope, "nodes": nodes, "edges": edges,
        "selected_node_ids": selected, "history": history,
    }
    if page_context is not None:
        payload["page_context"] = page_context
    if ip12_context is not None:
        payload["ip12_context"] = ip12_context
    raw = json.dumps(payload, ensure_ascii=False).lower()
    if any(marker in raw for marker in ("data:image/", "data:video/", ";base64,", "blob:")) or _CANVAS_BASE64_RE.search(raw):
        raise CLIAPIError(400, "画布上下文不能包含媒体数据或 Blob 地址")
    return payload


def _canvas_params(value, require_text=False):
    _strict_object(value, {"title", "text"}, ("text",) if require_text else ())
    if not value:
        raise CLIAPIError(400, "params 不能为空")
    params = {}
    if "title" in value:
        params["title"] = _string(value["title"], "params.title", 0, 120)
    if "text" in value:
        params["text"] = _string(value["text"], "params.text", 1 if require_text else 0, 5000)
    return params


def _canvas_ops_payload(value):
    _strict_object(value, {"board_id", "base_version", "op_id", "ops"},
                   ("board_id", "base_version", "op_id", "ops"))
    op_id = _string(value["op_id"], "op_id", 17, 128)
    if not _CANVAS_OP_ID_RE.fullmatch(op_id):
        raise CLIAPIError(400, "op_id 必须以 hqcli- 开头并包含足够的随机字符")
    raw_ops = value["ops"]
    if not isinstance(raw_ops, list) or not 1 <= len(raw_ops) <= 12:
        raise CLIAPIError(400, "ops 必须包含 1-12 项")
    ops = []
    for raw in raw_ops:
        if not isinstance(raw, dict):
            raise CLIAPIError(400, "画布操作必须是对象")
        kind = raw.get("type")
        if kind == "node.create":
            _strict_object(raw, {"type", "node"}, ("type", "node"))
            node = raw["node"]
            _strict_object(node, {"id", "type", "x", "y", "params"}, ("id", "type", "x", "y", "params"))
            ops.append({"type": kind, "node": {
                "id": _canvas_op_node_id(node["id"]),
                "type": _enum(node["type"], "node.type", ("text", "gen", "video")),
                "x": _number(node["x"], "node.x", 0, 100000),
                "y": _number(node["y"], "node.y", 0, 100000),
                "params": _canvas_params(node["params"], require_text=True),
            }})
        elif kind == "node.patch":
            _strict_object(raw, {"type", "id", "fields"}, ("type", "id", "fields"))
            fields = raw["fields"]
            _strict_object(fields, {"x", "y", "params"})
            if not fields:
                raise CLIAPIError(400, "node.patch fields 不能为空")
            clean = {}
            if "x" in fields:
                clean["x"] = _number(fields["x"], "fields.x", 0, 100000)
            if "y" in fields:
                clean["y"] = _number(fields["y"], "fields.y", 0, 100000)
            if "params" in fields:
                clean["params"] = _canvas_params(fields["params"])
            ops.append({"type": kind, "id": _canvas_op_node_id(raw["id"]), "fields": clean})
        elif kind == "edge.create":
            _strict_object(raw, {"type", "edge"}, ("type", "edge"))
            edge = raw["edge"]
            _strict_object(edge, {"from", "to"}, ("from", "to"))
            endpoints = {}
            for name in ("from", "to"):
                endpoint = edge[name]
                _strict_object(endpoint, {"node", "port"}, ("node", "port"))
                endpoints[name] = {
                    "node": _canvas_op_node_id(endpoint["node"], "edge.%s.node" % name),
                    "port": _enum(endpoint["port"], "edge.%s.port" % name, ("prompt", "image")),
                }
            if endpoints["from"]["node"] == endpoints["to"]["node"]:
                raise CLIAPIError(400, "画布连线不能形成自环")
            ops.append({"type": kind, "edge": endpoints})
        else:
            raise CLIAPIError(400, "CLI 不允许该画布操作")
    return {
        "board_id": _identifier(value["board_id"], "board_id"),
        "base_version": _integer(value["base_version"], "base_version", 1, 2**63 - 1),
        "op_id": op_id, "ops": ops,
    }


def _tags(value):
    if not isinstance(value, list) or len(value) > 8:
        raise CLIAPIError(400, "tags 必须是最多 8 项的数组")
    clean = []
    for item in value:
        tag = _string(item, "tags", 1, 24)
        if tag not in clean:
            clean.append(tag)
    return clean


def _normalize_scopes(value):
    if not isinstance(value, list) or not value or len(value) > len(SCOPES):
        raise CLIAPIError(400, "requested_scopes 必须是非空权限数组")
    scopes = []
    for item in value:
        if not isinstance(item, str) or item not in SCOPES:
            raise CLIAPIError(400, "包含未知权限范围")
        if item not in scopes:
            scopes.append(item)
    return scopes


def _allow_device_start(client_key, now):
    with _START_HITS_LOCK:
        hits = [stamp for stamp in _START_HITS.get(client_key, []) if now - stamp < 600]
        _START_HITS[client_key] = hits
        if len(hits) >= 10:
            return False
        hits.append(now)
        return True


def _user_code():
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    raw = "".join(secrets.choice(alphabet) for _ in range(8))
    return raw[:4] + "-" + raw[4:]


def _normalize_user_code(value):
    raw = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()
    if len(raw) != 8:
        raise CLIAPIError(400, "授权码格式不正确")
    return raw[:4] + "-" + raw[4:]


def start_device(db_factory, body, client_key, now=None):
    now = int(time.time() if now is None else now)
    _strict_object(body, {"client_name", "requested_scopes"}, ("client_name", "requested_scopes"))
    if not _allow_device_start(str(client_key or "unknown"), now):
        raise CLIAPIError(429, "授权请求过于频繁，请稍后重试", "rate_limited")
    client_name = _string(body["client_name"], "client_name", 1, 80)
    scopes = _normalize_scopes(body["requested_scopes"])
    for _ in range(16):
        device_code, user_code = secrets.token_urlsafe(32), _user_code()
        try:
            with db_factory() as connection:
                connection.execute(
                    """INSERT INTO cli_device_grants(
                       device_code_hash,user_code_hash,client_name,requested_scopes_json,status,created_at,expires_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (_hash(device_code), _hash(user_code), client_name,
                     json.dumps(scopes, separators=(",", ":")), "pending", now, now + DEVICE_TTL),
                )
            break
        except Exception as exc:
            if "UNIQUE" not in str(exc).upper():
                raise
    else:
        raise CLIAPIError(503, "暂时无法创建授权码，请重试")
    return {
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": PUBLIC_ORIGIN + "/workbench/device?user_code=" + urllib.parse.quote(user_code),
        "expires_in": DEVICE_TTL,
        "interval": POLL_INTERVAL,
        "scopes": scopes,
        "scope_details": [{"scope": scope, "description": SCOPES[scope]} for scope in scopes],
    }


def approve_device(db_factory, username, body, now=None):
    now = int(time.time() if now is None else now)
    _strict_object(body, {"user_code", "approve"}, ("user_code", "approve"))
    if not isinstance(body["approve"], bool):
        raise CLIAPIError(400, "approve 必须是布尔值")
    code_hash = _hash(_normalize_user_code(body["user_code"]))
    with db_factory() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM cli_device_grants WHERE user_code_hash=?", (code_hash,)).fetchone()
        if not row:
            raise CLIAPIError(404, "授权码不存在", "not_found")
        if int(row["expires_at"]) <= now:
            connection.execute("UPDATE cli_device_grants SET status='expired' WHERE id=?", (row["id"],))
            raise CLIAPIError(410, "授权码已过期", "expired_token")
        if row["status"] != "pending":
            if row["status"] == "approved" and row["username"] == username:
                return {"ok": True, "status": "approved"}
            raise CLIAPIError(409, "授权码已处理", "already_processed")
        status = "approved" if body["approve"] else "denied"
        scopes = row["requested_scopes_json"] if body["approve"] else None
        connection.execute(
            """UPDATE cli_device_grants
               SET status=?,username=?,approved_scopes_json=?,approved_at=? WHERE id=?""",
            (status, username, scopes, now, row["id"]),
        )
    return {"ok": True, "status": status}


def device_info(db_factory, body, now=None):
    now = int(time.time() if now is None else now)
    _strict_object(body, {"user_code"}, ("user_code",))
    code_hash = _hash(_normalize_user_code(body["user_code"]))
    with db_factory() as connection:
        row = connection.execute(
            "SELECT client_name,requested_scopes_json,status,expires_at FROM cli_device_grants WHERE user_code_hash=?",
            (code_hash,),
        ).fetchone()
    if not row:
        raise CLIAPIError(404, "授权码不存在", "not_found")
    status = row["status"]
    if status == "pending" and int(row["expires_at"]) <= now:
        status = "expired"
    try:
        scopes = json.loads(row["requested_scopes_json"] or "[]")
    except Exception:
        raise CLIAPIError(500, "授权请求数据无效", "invalid_grant")
    return {
        "client_name": row["client_name"], "status": status, "expires_at": int(row["expires_at"]),
        "scopes": scopes,
        "scope_details": [{"scope": scope, "description": SCOPES[scope]} for scope in scopes if scope in SCOPES],
    }


def poll_device(db_factory, body, now=None):
    now = int(time.time() if now is None else now)
    _strict_object(body, {"device_code"}, ("device_code",))
    device_code = _string(body["device_code"], "device_code", 20, 200)
    with db_factory() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM cli_device_grants WHERE device_code_hash=?", (_hash(device_code),)).fetchone()
        if not row:
            raise CLIAPIError(400, "设备授权请求无效", "invalid_grant")
        status = row["status"]
        if status in {"pending", "approved"} and int(row["expires_at"]) <= now:
            connection.execute("UPDATE cli_device_grants SET status='expired' WHERE id=?", (row["id"],))
            raise CLIAPIError(410, "设备授权已过期", "expired_token")
        if status == "approved":
            token = secrets.token_urlsafe(32)
            scopes = json.loads(row["approved_scopes_json"] or "[]")
            connection.execute(
                """UPDATE cli_device_grants SET status='issued',token_hash=?,token_expires_at=?,last_poll_at=?
                   WHERE id=? AND status='approved'""",
                (_hash(token), now + TOKEN_TTL, now, row["id"]),
            )
            return {"access_token": token, "token_type": "Bearer", "expires_in": TOKEN_TTL, "scopes": scopes}
        if status == "pending":
            last_poll = int(row["last_poll_at"] or 0)
            if last_poll and now - last_poll < POLL_INTERVAL:
                raise CLIAPIError(429, "轮询过快，请按 interval 重试", "slow_down")
            connection.execute("UPDATE cli_device_grants SET last_poll_at=? WHERE id=?", (now, row["id"]),)
            raise CLIAPIError(202, "等待用户授权", "authorization_pending")
        if status == "denied":
            raise CLIAPIError(403, "用户拒绝了授权", "access_denied")
        if status == "expired":
            raise CLIAPIError(410, "设备授权已过期", "expired_token")
        raise CLIAPIError(409, "访问令牌已经签发，请重新登录", "already_issued")


def authenticate(db_factory, token, now=None):
    now = int(time.time() if now is None else now)
    token = str(token or "").strip()
    if not 20 <= len(token) <= 200:
        return None
    with db_factory() as connection:
        row = connection.execute(
            """SELECT u.*,g.approved_scopes_json AS cli_scopes,g.token_expires_at AS cli_expires_at
               FROM cli_device_grants g JOIN users u ON u.username=g.username
               WHERE g.token_hash=? AND g.status='issued' AND g.revoked_at IS NULL
                 AND g.token_expires_at>? AND COALESCE(u.account_status,'active')='active'""",
            (_hash(token), now),
        ).fetchone()
    if not row:
        return None
    try:
        scopes = tuple(json.loads(row["cli_scopes"] or "[]"))
    except Exception:
        return None
    return row, scopes


def revoke(db_factory, token, now=None):
    now = int(time.time() if now is None else now)
    token = str(token or "").strip()
    if not token:
        return False
    with db_factory() as connection:
        cursor = connection.execute(
            "UPDATE cli_device_grants SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
            (now, _hash(token)),
        )
    return cursor.rowcount > 0


def origin_allowed(origin):
    return bool(origin) and hmac.compare_digest(str(origin).strip().rstrip("/"), PUBLIC_ORIGIN)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def proxy_json(plan, web_token, internal_token=""):
    headers = {
        "Authorization": "Bearer " + web_token,
        "User-Agent": "huangque-auth-cli-gateway/1",
        "Accept": "application/json",
    }
    headers.update(plan.get("headers") or {})
    if plan.get("internal"):
        if not internal_token:
            raise CLIAPIError(503, "CLI 内部授权未配置", "not_configured")
        headers["X-HQ-Internal-Token"] = internal_token
    body = plan.get("body")
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        plan["base"] + plan["path"], data=data, headers=headers, method=plan.get("method", "GET"),
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=plan.get("timeout", 30)) as response:
            raw, status = response.read(2 * 1024 * 1024 + 1), response.getcode()
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(2 * 1024 * 1024 + 1), exc.code
    except (urllib.error.URLError, OSError) as exc:
        raise CLIAPIError(502, "黄雀业务服务暂时不可用：" + str(exc)[:120], "upstream_unavailable")
    if len(raw) > 2 * 1024 * 1024:
        raise CLIAPIError(502, "黄雀业务服务响应过大", "upstream_response_too_large")
    try:
        payload = json.loads(raw or b"{}")
    except Exception:
        payload = {"detail": "黄雀业务服务返回了无效响应"}
        status = 502
    if isinstance(payload, dict) and "detail" not in payload and isinstance(payload.get("error"), str):
        payload = dict(payload, detail=payload["error"])
    return int(status), payload


def _proxy_media_upload(stream, length, web_token, internal_token, content_type, digest,
                        path, digest_header, label, extra_headers=None):
    display = {"image": "图片", "video": "视频", "audio": "音频"}[label]
    if not internal_token:
        raise CLIAPIError(503, "CLI 内部授权未配置", "not_configured")
    target = urllib.parse.urlsplit(CONTENT_BASE)
    if target.scheme != "http" or target.hostname not in {"127.0.0.1", "localhost"} or target.path not in {"", "/"}:
        raise CLIAPIError(503, "CLI %s上传目标配置不安全" % display, "not_configured")
    connection = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=60)
    try:
        connection.putrequest("POST", path, skip_accept_encoding=True)
        connection.putheader("Authorization", "Bearer " + web_token)
        connection.putheader("X-HQ-Internal-Token", internal_token)
        connection.putheader(digest_header, digest)
        connection.putheader("Content-Type", content_type)
        connection.putheader("Content-Length", str(length))
        connection.putheader("Accept", "application/json")
        connection.putheader("User-Agent", "huangque-auth-cli-upload/1")
        for key, value in (extra_headers or {}).items():
            connection.putheader(key, value)
        connection.endheaders()
        remaining = length
        while remaining:
            chunk = stream.read(min(64 * 1024, remaining))
            if not chunk:
                raise CLIAPIError(400, "%s上传不完整" % display, "invalid_%s_upload" % label)
            connection.send(chunk)
            remaining -= len(chunk)
        response = connection.getresponse()
        raw, status = response.read(2 * 1024 * 1024 + 1), response.status
    except CLIAPIError:
        raise
    except (OSError, http.client.HTTPException) as exc:
        raise CLIAPIError(502, "%s上传服务暂时不可用：" % display + str(exc)[:120], "upstream_unavailable")
    finally:
        connection.close()
    if len(raw) > 2 * 1024 * 1024:
        raise CLIAPIError(502, "%s上传服务响应过大" % display, "upstream_response_too_large")
    try:
        payload = json.loads(raw or b"{}")
    except Exception:
        raise CLIAPIError(502, "%s上传服务返回了无效响应" % display, "invalid_upstream_response")
    if not isinstance(payload, dict):
        raise CLIAPIError(502, "%s上传服务返回了无效响应" % display, "invalid_upstream_response")
    return int(status), payload


def proxy_image_upload(stream, length, web_token, internal_token, content_type, digest):
    return _proxy_media_upload(
        stream, length, web_token, internal_token, content_type, digest,
        "/api/gen/cli/image-upload", "X-HQ-Image-SHA256", "image",
    )


def proxy_video_upload(stream, length, web_token, internal_token, content_type, digest):
    return _proxy_media_upload(
        stream, length, web_token, internal_token, content_type, digest,
        "/api/gen/cli/video-upload", "X-HQ-Video-SHA256", "video",
    )


def proxy_video_import(stream, length, web_token, internal_token, content_type, digest, title):
    return _proxy_media_upload(
        stream, length, web_token, internal_token, content_type, digest,
        "/api/gen/video/import", "X-HQ-Video-SHA256", "video",
        extra_headers={"X-Video-Title": urllib.parse.quote(str(title or "")[:160], safe="._-")},
    )


def proxy_audio_upload(stream, length, web_token, internal_token, content_type, digest):
    return _proxy_media_upload(
        stream, length, web_token, internal_token, content_type, digest,
        "/api/gen/cli/audio-upload", "X-HQ-Audio-SHA256", "audio",
    )


def proxy_digital_human_material_upload(
        stream, length, web_token, internal_token, content_type, digest):
    return _proxy_media_upload(
        stream, length, web_token, internal_token, content_type, digest,
        "/api/gen/script_to_video/material-upload", "X-HQ-Image-SHA256", "image",
    )


def proxy_digital_human_audio_upload(
        stream, length, web_token, internal_token, content_type, digest, run_id):
    run_id = _matched_string(run_id, "run_id", _DIGITAL_HUMAN_RUN_RE, 135)
    return _proxy_media_upload(
        stream, length, web_token, internal_token, content_type, digest,
        "/api/gen/digital-human-v2/audio-upload", "X-HQ-Audio-SHA256", "audio",
        extra_headers={"X-HQ-Run-ID": run_id},
    )


def proxy_director_breakdown_upload(stream, length, web_token, internal_token,
                                    content_type, digest, media_type, filename,
                                    idempotency_key, expected_cost):
    if media_type not in {"image", "video"}:
        raise CLIAPIError(400, "编导反推上传类型无效", "invalid_breakdown_upload")
    digest_header = "X-HQ-Image-SHA256" if media_type == "image" else "X-HQ-Video-SHA256"
    return _proxy_media_upload(
        stream, length, web_token, internal_token, content_type, digest,
        "/api/gen/breakdown/local-upload?media_type=" + media_type,
        digest_header, media_type,
        {
            "X-File-Name": urllib.parse.quote(filename, safe="._-"),
            "Idempotency-Key": idempotency_key,
            "X-HQ-Expected-Cost": str(int(expected_cost)),
        },
    )


def director_breakdown_upload_descriptor(value):
    """Normalize the immutable fields shared by upload quote and confirmation."""
    _strict_object(value, {"media_type", "sha256"}, ("media_type", "sha256"))
    media_type = _enum(value["media_type"], "media_type", ("image", "video"))
    digest = str(value["sha256"] or "").strip().lower()
    if not _SHA256_RE.fullmatch(digest):
        raise CLIAPIError(400, "sha256 必须是 64 位十六进制摘要", "invalid_upload_digest")
    return {"media_type": media_type, "sha256": digest}


def _plan(scope, kind, **values):
    return {"scope": scope, "kind": kind, **values}


def _idempotent_proxy(scope, path, value, allowed, required, *, extra_scopes=()):
    _strict_object(value, set(allowed) | {"request_id"}, tuple(required) + ("request_id",))
    request_id = _matched_string(
        value["request_id"], "request_id", _IDEMPOTENCY_KEY_RE, 128,
    )
    return _plan(
        scope, "proxy", base=CONTENT_BASE, path=path, method="POST",
        body={key: json.loads(json.dumps(item, ensure_ascii=False))
              for key, item in value.items() if key != "request_id"},
        headers={"Idempotency-Key": request_id},
        extra_scopes=tuple(extra_scopes),
    )


def _matched_string(value, field, pattern, maximum=160):
    value = _string(value, field, 1, maximum)
    if not pattern.fullmatch(value):
        raise CLIAPIError(400, field + " 格式不合法")
    return value


def validate_idempotency_key(value):
    """Validate a first-party idempotency key without exposing the regex."""
    return _matched_string(value, "idempotency_key", _IDEMPOTENCY_KEY_RE, 128)


def _video_compose_decisions(value):
    if not isinstance(value, dict) or not 1 <= len(value) <= 200:
        raise CLIAPIError(400, "decisions 必须是包含 1-200 项的对象")
    decisions = {}
    for candidate_id, decision in value.items():
        candidate_id = _matched_string(candidate_id, "候选片段 ID", _VIDEO_COMPOSE_CANDIDATE_RE)
        decisions[candidate_id] = _enum(decision, "剪辑决定", ("keep", "remove"))
    return decisions


def _digital_presenter_fields(value):
    fields = {}
    if "title" in value:
        fields["title"] = _string(value["title"], "title", 1, 80)
    if "script_text" in value:
        fields["script_text"] = _string(value["script_text"], "script_text", 0, 20000)
    if "ratio" in value:
        fields["ratio"] = _enum(value["ratio"], "ratio", ("9:16", "16:9"))
    if "resolution" in value:
        fields["resolution"] = _enum(value["resolution"], "resolution", ("1080p",))
    if "voice_key" in value:
        fields["voice_key"] = _string(value["voice_key"], "voice_key", 0, 200)
    if "target_duration" in value:
        fields["target_duration"] = _integer(value["target_duration"], "target_duration", 30, 180)
    return fields


def _text_video_base_payload(value, extra_fields=()):
    allowed = {"text", "template", "mode", "style", "voice", "speech_rate"} | set(extra_fields)
    _strict_object(value, allowed, ("text", "template", "style", "voice"))
    return {
        "pipeline": "pixelle",
        "text": _string(value["text"], "text", 2, 1000),
        "template": _string(value["template"], "template", 1, 240),
        "mode": _enum(value.get("mode", "generate"), "mode", ("generate", "fixed")),
        "style": _string(value["style"], "style", 1, 80),
        "voice": _string(value["voice"], "voice", 1, 200),
        "speech_rate": float(Decimal(str(
            _number(value.get("speech_rate", 1.0), "speech_rate", 0.5, 2.0)
        )).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)),
        "source_page": "text-video",
    }


def _text_video_talking_material(raw):
    _strict_object(raw, {
        "enabled", "plan_id", "source_hash", "ratio",
        "default_avatar_asset_id", "scenes",
    }, ("enabled", "plan_id", "source_hash", "ratio", "default_avatar_asset_id", "scenes"))
    if raw["enabled"] is not True:
        raise CLIAPIError(400, "talking_material.enabled 必须为 true")
    scenes = raw["scenes"]
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= 20:
        raise CLIAPIError(400, "talking_material.scenes 必须包含 1-20 项")
    normalized_scenes = []
    seen = set()
    for item in scenes:
        _strict_object(item, {"scene_id", "enabled", "avatar_asset_id"},
                       ("scene_id", "enabled"))
        scene_id = _matched_string(
            item["scene_id"], "scene_id", _TALKING_SCENE_ID_RE, 16)
        if scene_id in seen:
            raise CLIAPIError(400, "talking_material.scenes 不能重复")
        seen.add(scene_id)
        if not isinstance(item["enabled"], bool):
            raise CLIAPIError(400, "talking_material.scenes.enabled 必须是布尔值")
        scene = {"scene_id": scene_id, "enabled": item["enabled"]}
        if "avatar_asset_id" in item:
            if not item["enabled"]:
                raise CLIAPIError(400, "未启用的口播分镜不能指定人物")
            scene["avatar_asset_id"] = _matched_string(
                item["avatar_asset_id"], "avatar_asset_id", _TALKING_AVATAR_ID_RE, 64)
        normalized_scenes.append(scene)
    if not any(item["enabled"] for item in normalized_scenes):
        raise CLIAPIError(400, "请至少启用一个口播分镜")
    return {
        "enabled": True,
        "plan_id": _matched_string(raw["plan_id"], "plan_id", _TALKING_PLAN_ID_RE, 64),
        "source_hash": _matched_string(raw["source_hash"], "source_hash", _SHA256_RE, 64),
        "ratio": _number(raw["ratio"], "ratio", 0.1, 0.5),
        "default_avatar_asset_id": _matched_string(
            raw["default_avatar_asset_id"], "default_avatar_asset_id",
            _TALKING_AVATAR_ID_RE, 64),
        "scenes": normalized_scenes,
    }


def _text_video_payload(value):
    payload = _text_video_base_payload(value, ("talking_material",))
    if "talking_material" in value:
        payload["talking_material"] = _text_video_talking_material(
            value["talking_material"])
    return payload


def _matrix_template_payload(value):
    _strict_object(
        value, {"top_text", "bottom_text", "template_id", "font_family"},
        ("top_text", "bottom_text", "template_id"),
    )
    template_id = _string(value["template_id"], "template_id", 1, 64)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", template_id):
        raise CLIAPIError(400, "template_id 格式不合法")
    result = {
        "top_text": _string(value["top_text"], "top_text", 2, 60),
        "bottom_text": _string(value["bottom_text"], "bottom_text", 2, 80),
        "template_id": template_id,
        "bgm": True,
    }
    if "font_family" in value:
        font_family = _string(value["font_family"], "font_family", 0, 80)
        if font_family:
            result["font_family"] = font_family
    return result


def _matrix_template_batch_payload(value):
    _strict_object(
        value, {"top_text", "bottom_text", "template_id", "font_family", "count"},
        ("top_text", "bottom_text", "template_id", "count"),
    )
    count = _integer(value["count"], "count", 2, 5)
    item = _matrix_template_payload({
        key: field for key, field in value.items() if key != "count"
    })
    return {"item": item, "count": count}


def _collect_url(value, allow_twitter=False):
    url = _string(value, "url", 1, 2048)
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        raise CLIAPIError(400, "url 格式不合法")
    allowed = ("douyin.com", "iesdouyin.com", "xiaohongshu.com", "xhslink.com", "xhslink.cn",
               "bilibili.com", "b23.tv")
    if allow_twitter:
        allowed += ("x.com", "twitter.com")
    channels_share = (parsed.scheme == "https" and host == "weixin.qq.com" and port in (None, 443)
                      and parsed.path.startswith("/sph/") and parsed.path[len("/sph/"):].isalnum())
    if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
            or port not in (None, 80, 443)
            or not (channels_share or any(
                host == suffix or host.endswith("." + suffix) for suffix in allowed))):
        raise CLIAPIError(400, "url 仅支持抖音、小红书、视频号、B 站或 X 单帖公开链接")
    return url


def _leads_payload(value):
    _strict_object(value, {"keyword", "platforms", "count", "pages", "channels_targets"},
                   ("platforms",))
    raw_platforms = value["platforms"]
    if not isinstance(raw_platforms, list) or not 1 <= len(raw_platforms) <= 3:
        raise CLIAPIError(400, "platforms 必须是包含 1-3 项的平台数组")
    platforms = [_enum(item, "platforms", ("douyin", "xhs", "channels")) for item in raw_platforms]
    if len(platforms) != len(set(platforms)):
        raise CLIAPIError(400, "platforms 不能重复")
    keyword = _string(value.get("keyword", ""), "keyword", 0, 120)
    raw_targets = value.get("channels_targets", [])
    if not isinstance(raw_targets, list) or len(raw_targets) > 20:
        raise CLIAPIError(400, "channels_targets 必须是最多 20 项的数组")
    targets = [_string(item, "channels_targets", 1, 120) for item in raw_targets]
    if len(targets) != len(set(targets)):
        raise CLIAPIError(400, "channels_targets 不能重复")
    if any(platform != "channels" for platform in platforms) and not keyword:
        raise CLIAPIError(400, "抖音或小红书获客必须提供 keyword")
    if "channels" in platforms and not targets:
        raise CLIAPIError(400, "视频号获客必须提供 channels_targets")
    return {
        "keyword": keyword, "platforms": platforms,
        "count": _integer(value.get("count", 20), "count", 1, 30),
        "pages": _integer(value.get("pages", 1), "pages", 1, 3),
        "channels_targets": targets,
    }


def _director_image_scenes(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise CLIAPIError(400, "scenes 必须包含 1-8 个分镜")
    normalized = []
    for item in value:
        _strict_object(item, {"scene", "line", "dur"})
        scene = {}
        if "scene" in item:
            scene["scene"] = _string(item["scene"], "scene", 0, 2000)
        if "line" in item:
            scene["line"] = _string(item["line"], "line", 0, 2000)
        if "dur" in item:
            scene["dur"] = _number(item["dur"], "dur", 0.1, 180)
        normalized.append(scene)
    if not any(item.get("scene", "").strip() for item in normalized):
        raise CLIAPIError(400, "至少一个分镜必须包含画面描述 scene")
    return normalized


def _director_storyboard(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 60:
        raise CLIAPIError(400, "storyboard 必须包含 1-60 个分镜")
    if any(not isinstance(item, dict) for item in value):
        raise CLIAPIError(400, "storyboard 分镜必须是 JSON 对象")
    return json.loads(json.dumps(value, ensure_ascii=False))


def _web_parity_scope(action, method):
    write = method != "GET"
    if action.startswith("creator-agent-"):
        return "creator-agent:%s" % ("write" if write else "read")
    if action == "task-delete":
        return "tasks:write"
    if action.startswith("digital-human-oneclick-"):
        return "digital-human-oneclick:%s" % (
            "read" if action.endswith("preflight") else "write"
        )
    if action.startswith("short-drama-"):
        return "short-drama:%s" % ("write" if write else "read")
    if action.startswith("digital-ip-"):
        return "ip12:%s" % ("write" if write else "read")
    if action.startswith("canvas-member"):
        return "canvas:%s" % ("write" if write else "read")
    if action.startswith(("asset-", "avatar-", "voice-", "video-import")):
        return "assets:%s" % ("write" if write else "read")
    return "account:%s" % ("write" if write else "read")


def _web_parity_value(action, name, value):
    if name == "notification_id":
        return _integer(value, name, 1, 2**63 - 1)
    if name in _WEB_PARITY_INTEGER_FIELDS:
        return _integer(value, name, 0, 2**63 - 1)
    if name in {"consent", "locked"}:
        if not isinstance(value, bool):
            raise CLIAPIError(400, "%s 必须是布尔值" % name)
        return value
    if name == "assets":
        if not isinstance(value, list) or not 1 <= len(value) <= 120:
            raise CLIAPIError(400, "assets 必须是 1-120 项的数组")
        normalized, seen = [], set()
        for item in value:
            _strict_object(item, {"kind", "id"}, ("kind", "id"))
            kind = _enum(item["kind"], "assets.kind", ("image", "audio", "video", "avatar"))
            asset_id = _integer(item["id"], "assets.id", 1, 2**63 - 1)
            if (kind, asset_id) not in seen:
                normalized.append({"kind": kind, "id": asset_id})
                seen.add((kind, asset_id))
        return normalized
    if name in _WEB_PARITY_ARRAY_FIELDS:
        if not isinstance(value, list) or len(value) > 120:
            raise CLIAPIError(400, "%s 必须是最多 120 项的数组" % name)
        if name in {"planning_messages", "shot_keys"}:
            return [_string(item, name, 1, 600) for item in value]
        return value
    if name in _WEB_PARITY_OBJECT_FIELDS:
        if not isinstance(value, dict):
            raise CLIAPIError(400, "%s 必须是 JSON 对象" % name)
        return value
    if name == "request_id":
        return _matched_string(value, name, _IDEMPOTENCY_KEY_RE, 128)
    if name == "role":
        return _enum(value, name, ("editor", "viewer"))
    if name == "action":
        return _enum(value, name, (("accept", "reject") if action == "friend-request-respond" else
                                   ("delete", "copy", "insert_before", "insert_after", "smart_insert", "move_up", "move_down")))
    if name == "source" and action in {"short-drama-scene-reference", "short-drama-character-reference-select"}:
        return _enum(value, name, ("asset", "upload"))
    if name == "ratio":
        return _enum(value, name, ("9:16", "16:9"))
    if name == "import_mode":
        return _enum(value, name, ("faithful", "optimize"))
    if name == "content_type":
        return _enum(value, name, ("live_action",))
    if name == "source_requirement":
        return _enum(value, name, ("", "complete_story"))
    if name == "mode" and action == "short-drama-refinement-media":
        return _enum(value, name, ("voice_timeline", "provider_audio", "silent"))
    if name == "digital_human_pipeline":
        return _enum(value, name, ("digital_human_material_v3",))
    if name == "digital_human_stage":
        return _enum(value, name, ("material_resolve",))
    if name == "digital_human_narration_mode":
        return _enum(value, name, ("text", "audio"))
    return _string(value, name, 1 if name.endswith("_id") or name in {"message", "title", "account_id", "username", "url", "product_type", "order_type", "action"} else 0, 8000)


def _web_parity_plan(action, value):
    base_kind, method, template, fields, required, _, idempotent = WEB_PARITY_ACTIONS[action]
    _strict_object(value, set(fields), required)
    body = {name: _web_parity_value(action, name, raw) for name, raw in value.items()}
    if action == "friend-request-respond":
        body["action"] = _enum(body["action"], "action", ("accept", "reject"))
    if action == "invite-users" and "level" in body:
        body["level"] = _integer(body["level"], "level", 1, 2)
    path_names = [name for name in fields if "{" + name + "}" in template and name != "query"]
    path = template
    for name in path_names:
        path = path.replace("{" + name + "}", urllib.parse.quote(str(body[name]), safe=""))
    for name in path_names:
        body.pop(name, None)
    if "{query}" in path:
        query_fields = fields if method == "GET" else ("project_id",)
        path = path.replace("{query}", urllib.parse.urlencode({name: body[name] for name in query_fields if name in body}))
        if method != "GET":
            for name in query_fields:
                body.pop(name, None)
    headers = {}
    if idempotent:
        headers["Idempotency-Key"] = body["request_id"]
        if action != "creator-agent-message":
            body.pop("request_id")
    base = CREATOR_AGENT_BASE if base_kind == _CREATOR else (CONTENT_BASE if base_kind == _CONTENT else PUBLIC_ORIGIN)
    return _plan(_web_parity_scope(action, method), "proxy", base=base, path=path,
                 method=method, body=None if method == "GET" else body, headers=headers)


def action_plan(action, value):
    if not isinstance(value, dict):
        raise CLIAPIError(400, "input 必须是 JSON 对象")
    if action not in ACTION_CATALOG_MAP:
        raise CLIAPIError(404, "未知 CLI 能力", "unknown_action")
    if action in WEB_PARITY_ACTIONS:
        return _web_parity_plan(action, value)
    if action == "account":
        _strict_object(value, set())
        return _plan("profile:read", "account")
    if action == "channels":
        _strict_object(value, set())
        return _plan("profile:read", "channels")
    if action == "pricing":
        _strict_object(value, set())
        return _plan("profile:read", "proxy", base=CONTENT_BASE, path="/api/gen/pricing")
    if action == "director-capability":
        _strict_object(value, set())
        return _plan("director:read", "director-capability")
    if action == "digital-human-oneclick-capability":
        _strict_object(value, set())
        return _plan(
            "digital-human-oneclick:read", "proxy", base=CONTENT_BASE,
            path="/api/gen/digital-human-v2/capability",
        )
    if action == "digital-human-oneclick-plan":
        _strict_object(value, {
            "script", "narration_mode", "audio_upload_id",
            "allow_ai_materials", "customer_upload_ids",
        }, ("narration_mode",))
        mode = _enum(value["narration_mode"], "narration_mode", ("text", "audio"))
        body = {
            "narration_mode": mode,
            "allow_ai_materials": value.get("allow_ai_materials", False),
            "customer_upload_ids": value.get("customer_upload_ids", []),
        }
        if not isinstance(body["allow_ai_materials"], bool):
            raise CLIAPIError(400, "allow_ai_materials 必须是布尔值")
        uploads = body["customer_upload_ids"]
        if not isinstance(uploads, list) or len(uploads) > 12:
            raise CLIAPIError(400, "customer_upload_ids 必须是最多 12 项的数组")
        body["customer_upload_ids"] = [
            _upload_id(item, "customer_upload_ids") for item in uploads
        ]
        if len(body["customer_upload_ids"]) != len(set(body["customer_upload_ids"])):
            raise CLIAPIError(400, "customer_upload_ids 不能重复")
        if mode == "text":
            if "audio_upload_id" in value:
                raise CLIAPIError(400, "text 模式不接受 audio_upload_id")
            body["script"] = _string(value.get("script"), "script", 12, 6000)
        else:
            if "script" in value:
                raise CLIAPIError(400, "audio 模式不接受 script")
            audio_id = _string(value.get("audio_upload_id"), "audio_upload_id", 36, 36)
            if not re.fullmatch(r"dha_[0-9a-f]{32}", audio_id):
                raise CLIAPIError(400, "audio_upload_id 格式不合法")
            body["audio_upload_id"] = audio_id
        return _plan(
            "digital-human-oneclick:read", "proxy", base=CONTENT_BASE,
            path="/api/gen/digital-human-v2/plan", method="POST", body=body,
        )
    if action == "digital-human-oneclick-consent":
        allowed = {
            "confirmed", "consent_version", "purpose", "run_id", "plan_digest",
            "script", "photo_sha256", "voice_mode", "voice_ref", "voice_sha256",
            "narration_mode", "audio_upload_id", "allow_ai_materials",
            "customer_upload_ids",
        }
        _strict_object(value, allowed, (
            "confirmed", "consent_version", "purpose", "run_id", "plan_digest",
            "photo_sha256", "voice_mode", "voice_ref", "narration_mode",
        ))
        if value.get("confirmed") is not True:
            raise CLIAPIError(400, "confirmed 必须为 true")
        body = json.loads(json.dumps(value, ensure_ascii=False))
        return _plan(
            "digital-human-oneclick:write", "proxy", base=CONTENT_BASE,
            path="/api/gen/digital-human-v2/consent", method="POST", body=body,
        )
    if action == "digital-human-oneclick-start":
        allowed = {
            "request_id", "consent_token", "plan_digest", "script",
            "narration_mode", "audio_upload_id", "allow_ai_materials",
            "customer_upload_ids", "portrait_upload_id", "voice_key",
        }
        _strict_object(value, allowed, (
            "request_id", "consent_token", "plan_digest", "narration_mode",
            "allow_ai_materials", "customer_upload_ids", "portrait_upload_id",
        ))
        payload = json.loads(json.dumps(value, ensure_ascii=False))
        _matched_string(payload["request_id"], "request_id", _IDEMPOTENCY_KEY_RE, 128)
        _matched_string(payload["plan_digest"], "plan_digest", _SHA256_RE, 64)
        _upload_id(payload["portrait_upload_id"], "portrait_upload_id")
        return _plan(
            "digital-human-oneclick:generate", "generation",
            generation_kind="digital_human_oneclick",
            endpoint="/api/gen/digital-human-v2/runs", payload=payload,
            quote_endpoint="/api/gen/digital-human-v2/runs/quote",
            quote_body=payload,
            quote_result_fields=("run_id", "request_id", "plan_digest", "cost_breakdown"),
        )
    if action in {
            "digital-human-oneclick-status", "digital-human-oneclick-recover",
            "digital-human-oneclick-abandon"}:
        required = ("run_id",) if action.endswith("status") else ("run_id", "request_id")
        _strict_object(value, set(required), required)
        run_id = _matched_string(value["run_id"], "run_id", _DIGITAL_HUMAN_RUN_RE, 135)
        path = "/api/gen/digital-human-v2/runs/" + urllib.parse.quote(run_id, safe="")
        if action.endswith("status"):
            return _plan(
                "digital-human-oneclick:read", "proxy", base=CONTENT_BASE,
                path=path,
            )
        request_id = _matched_string(
            value["request_id"], "request_id", _IDEMPOTENCY_KEY_RE, 128,
        )
        suffix = "/recover" if action.endswith("recover") else "/abandon"
        return _plan(
            "digital-human-oneclick:write", "proxy", base=CONTENT_BASE,
            path=path + suffix, method="POST", body={"request_id": request_id},
        )
    if action == "digital-human-oneclick-history":
        _strict_object(value, {"limit", "offset"})
        query = urllib.parse.urlencode({
            "limit": _integer(value.get("limit", 20), "limit", 1, 50),
            "offset": _integer(value.get("offset", 0), "offset", 0, 2000),
        })
        return _plan(
            "digital-human-oneclick:read", "proxy", base=CONTENT_BASE,
            path="/api/gen/digital-human-v2/history?" + query,
        )
    if action == "director-script-generate":
        _strict_object(value, {"prompt", "style", "duration", "platform"}, ("prompt",))
        style = _enum(value.get("style", "spoken"), "style", ("spoken", "story", "recommend"))
        platform = _enum(value.get("platform", "douyin"), "platform", ("douyin", "xiaohongshu", "channels"))
        duration = _integer(value.get("duration", 30), "duration", 15, 60)
        if duration not in (15, 30, 60):
            raise CLIAPIError(400, "duration 仅支持 15、30 或 60")
        payload = {
            "prompt": _string(value["prompt"], "prompt", 1, 20000),
            "format": "script",
            "style": {"spoken": "口播", "story": "剧情", "recommend": "种草"}[style],
            "dur": str(duration) + "s",
            "platform": {"douyin": "抖音", "xiaohongshu": "小红书", "channels": "视频号"}[platform],
            "ctype": "分镜脚本",
            "source_page": "script",
        }
        return _plan(
            "director:generate", "generation", generation_kind="copy",
            endpoint="/api/gen/copy", payload=payload,
            submit_headers={"X-HQ-Submission-Class": "director-agent"},
        )
    if action == "director-breakdown":
        _strict_object(value, {"url", "urls", "mode"})
        has_url, has_urls = "url" in value, "urls" in value
        if has_url == has_urls:
            raise CLIAPIError(400, "必须且只能提供 url 或 urls")
        mode = _enum(value.get("mode", "scenes"), "mode", ("scenes", "reverse_prompt"))
        payload = {"mode": mode, "source_page": "script"}
        if has_url:
            payload["url"] = _string(value["url"], "url", 1, 2000)
        else:
            urls = value["urls"]
            if not isinstance(urls, list) or not 1 <= len(urls) <= 5:
                raise CLIAPIError(400, "urls 必须包含 1-5 条链接")
            payload["urls"] = [_string(item, "urls", 1, 2000) for item in urls]
            if len(set(payload["urls"])) != len(payload["urls"]):
                raise CLIAPIError(400, "urls 不能重复")
        if mode == "reverse_prompt" and len(payload.get("urls", [payload.get("url")])) != 1:
            raise CLIAPIError(400, "reverse_prompt 仅支持单条链接")
        return _plan("director:generate", "generation", generation_kind="breakdown",
                     endpoint="/api/gen/breakdown", payload=payload)
    if action == "director-scene-image-generate":
        _strict_object(value, {"scenes", "ratio", "quality"}, ("scenes",))
        scenes = _director_image_scenes(value["scenes"])
        prompt = "，".join(item["scene"].strip() for item in scenes
                           if item.get("scene", "").strip())
        payload = {
            "prompt": prompt,
            "ratio": _enum(value.get("ratio", "9:16"), "ratio",
                           ("9:16", "16:9", "1:1", "4:5", "5:4")),
            "quality": _enum(value.get("quality", "standard"), "quality",
                             ("standard", "hd")),
            "source_page": "script",
        }
        return _plan("director:generate", "generation", generation_kind="image",
                     endpoint="/api/gen/image", payload=payload)
    if action == "director-scene-video-generate":
        _strict_object(value, {
            "scenes", "channel", "ratio", "duration", "seconds", "resolution",
            "model", "generate_audio", "reference_upload_ids",
        }, ("scenes",))
        scenes = _director_image_scenes(value["scenes"])
        payload = {key: item for key, item in value.items() if key != "scenes"}
        payload["prompt"] = "，".join(
            item["scene"].strip() for item in scenes if item.get("scene", "").strip()
        )
        plan = action_plan("video-generate", payload)
        plan["scope"] = "director:generate"
        return plan
    if action == "director-scene-talking-generate":
        plan = action_plan("text-video-generate", value)
        plan["scope"] = "director:generate"
        return plan
    if action == "director-workflows":
        _strict_object(value, {"limit", "offset"})
        query = urllib.parse.urlencode({
            "limit": _integer(value.get("limit", 20), "limit", 1, 50),
            "offset": _integer(value.get("offset", 0), "offset", 0, 2000),
        })
        return _plan("director:read", "proxy", base=CONTENT_BASE,
                     path="/api/gen/director/workflows?" + query)
    if action == "director-workflow-create":
        allowed = {"title", "source_job_id", "storyboard"}
        plan = _idempotent_proxy(
            "director:write", "/api/gen/director/workflows", value,
            allowed, ("title",),
        )
        body = plan["body"]
        body["title"] = _string(body["title"], "title", 1, 120)
        if ("source_job_id" in body) == ("storyboard" in body):
            raise CLIAPIError(400, "必须且只能提供 source_job_id 或 storyboard")
        if "source_job_id" in body:
            body["source_job_id"] = _integer(body["source_job_id"], "source_job_id", 1, 2**63 - 1)
        else:
            body["storyboard"] = _director_storyboard(body["storyboard"])
        return plan
    if action in {"director-workflow", "director-storyboard-export"}:
        _strict_object(value, {"workflow_id"}, ("workflow_id",))
        workflow_id = _matched_string(
            value["workflow_id"], "workflow_id", _DIRECTOR_WORKFLOW_RE, 35,
        )
        suffix = "/storyboard/export" if action.endswith("export") else ""
        return _plan("director:read", "proxy", base=CONTENT_BASE,
                     path="/api/gen/director/workflows/%s%s" % (workflow_id, suffix))
    if action == "director-storyboard-update":
        _strict_object(value, {"workflow_id", "revision", "storyboard"},
                       ("workflow_id", "revision", "storyboard"))
        workflow_id = _matched_string(
            value["workflow_id"], "workflow_id", _DIRECTOR_WORKFLOW_RE, 35,
        )
        return _plan(
            "director:write", "proxy", base=CONTENT_BASE,
            path="/api/gen/director/workflows/%s/storyboard" % workflow_id,
            method="PUT", body={
                "revision": _integer(value["revision"], "revision", 1, 2**63 - 1),
                "storyboard": _director_storyboard(value["storyboard"]),
            },
        )
    if action in {"director-production-plan", "director-remake-plan"}:
        workflow_id = _matched_string(
            value.get("workflow_id"), "workflow_id", _DIRECTOR_WORKFLOW_RE, 35,
        )
        if action == "director-production-plan":
            _strict_object(value, {"workflow_id", "output_kind", "options"},
                           ("workflow_id", "output_kind", "options"))
            body = {
                "output_kind": _enum(value["output_kind"], "output_kind", ("image", "video")),
                "options": value["options"],
            }
        else:
            _strict_object(value, {"workflow_id", "mode", "instruction", "options"},
                           ("workflow_id", "mode", "instruction", "options"))
            body = {
                "mode": _enum(value["mode"], "mode", ("cinematic", "grok", "micro")),
                "instruction": _string(value["instruction"], "instruction", 1, 2000),
                "options": value["options"],
            }
        if not isinstance(body["options"], dict):
            raise CLIAPIError(400, "options 必须是 JSON 对象")
        kind = "production" if "production" in action else "remake"
        return _plan(
            "director:write", "proxy", base=CONTENT_BASE,
            path="/api/gen/director/workflows/%s/%s/plan" % (workflow_id, kind),
            method="POST", body=json.loads(json.dumps(body, ensure_ascii=False)),
        )
    if action in {"director-production-start", "director-remake-start"}:
        _strict_object(value, {"workflow_id", "plan_digest", "request_id"},
                       ("workflow_id", "plan_digest", "request_id"))
        workflow_id = _matched_string(
            value["workflow_id"], "workflow_id", _DIRECTOR_WORKFLOW_RE, 35,
        )
        payload = {
            "plan_digest": _matched_string(value["plan_digest"], "plan_digest", _SHA256_RE, 64),
            "request_id": _matched_string(value["request_id"], "request_id", _IDEMPOTENCY_KEY_RE, 128),
        }
        kind = "production" if "production" in action else "remake"
        base = "/api/gen/director/workflows/%s/%s" % (workflow_id, kind)
        return _plan(
            "director:generate", "generation",
            generation_kind="director_" + kind,
            endpoint=base + "/start", payload=payload,
            quote_endpoint=base + "/quote", quote_body=payload,
            quote_result_fields=("workflow_id", "kind", "plan_digest", "request_id"),
        )
    if action in {"director-production-status", "director-remake-status"}:
        _strict_object(value, {"workflow_id"}, ("workflow_id",))
        workflow_id = _matched_string(
            value["workflow_id"], "workflow_id", _DIRECTOR_WORKFLOW_RE, 35,
        )
        kind = "production" if "production" in action else "remake"
        return _plan("director:read", "proxy", base=CONTENT_BASE,
                     path="/api/gen/director/workflows/%s/%s" % (workflow_id, kind))
    if action in {"director-production-recover", "director-remake-recover"}:
        _strict_object(value, {"workflow_id", "plan_digest", "request_id"},
                       ("workflow_id", "plan_digest", "request_id"))
        workflow_id = _matched_string(
            value["workflow_id"], "workflow_id", _DIRECTOR_WORKFLOW_RE, 35,
        )
        request_id = _matched_string(
            value["request_id"], "request_id", _IDEMPOTENCY_KEY_RE, 128,
        )
        body = {
            "plan_digest": _matched_string(value["plan_digest"], "plan_digest", _SHA256_RE, 64),
            "request_id": request_id,
        }
        kind = "production" if "production" in action else "remake"
        return _plan(
            "director:recover", "proxy", base=CONTENT_BASE,
            path="/api/gen/director/workflows/%s/%s/recover" % (workflow_id, kind),
            method="POST", body=body, headers={"Idempotency-Key": request_id},
            extra_scopes=("generation:submit",),
        )
    if action == "director-chat":
        allowed = {
            "prompt", "session_id", "page_revision", "page_context",
            "history", "source_page",
        }
        plan = _idempotent_proxy(
            "director:write", "/api/gen/director_agent", value, allowed,
            ("prompt", "session_id", "page_revision", "page_context"),
        )
        plan["body"]["prompt"] = _string(plan["body"]["prompt"], "prompt", 1, 2000)
        plan["body"]["session_id"] = _identifier(plan["body"]["session_id"], "session_id")
        plan["body"]["page_revision"] = _integer(
            plan["body"]["page_revision"], "page_revision", 1, 2**63 - 1,
        )
        if not isinstance(plan["body"]["page_context"], dict):
            raise CLIAPIError(400, "page_context 必须是 JSON 对象")
        if not isinstance(plan["body"].get("history", []), list):
            raise CLIAPIError(400, "history 必须是数组")
        plan["body"]["quoted_cost"] = 0
        return plan
    if action == "director-produce":
        required = {"offer_id", "input", "expected_cost", "plan_digest", "quote_token"}
        _strict_object(value, required, tuple(required))
        offer_id = _string(value["offer_id"], "offer_id", 36, 84)
        if not re.fullmatch(r"director-production-[A-Za-z0-9_-]{16,64}", offer_id):
            raise CLIAPIError(400, "offer_id 格式不合法")
        body = json.loads(json.dumps(value, ensure_ascii=False))
        if not isinstance(body["input"], dict):
            raise CLIAPIError(400, "input 必须是 JSON 对象")
        _integer(body["expected_cost"], "expected_cost", 0, 10**9)
        _matched_string(body["plan_digest"], "plan_digest", _SHA256_RE, 64)
        _string(body["quote_token"], "quote_token", 20, 4096)
        return _plan(
            "director:generate", "proxy", base=CONTENT_BASE,
            path="/api/gen/director_agent/produce", method="POST", body=body,
            headers={"Idempotency-Key": offer_id},
            extra_scopes=("generation:submit",),
        )
    if action == "short-drama-advisor":
        allowed = {
            "messages", "understanding", "expected_field", "field_states",
            "recommendation_context", "user_message",
        }
        plan = _idempotent_proxy(
            "short-drama:write", "/api/gen/short-drama/advisor", value,
            allowed, ("user_message",),
        )
        plan["body"]["user_message"] = _string(
            plan["body"]["user_message"], "user_message", 1, 600,
        )
        if not isinstance(plan["body"].get("messages", []), list):
            raise CLIAPIError(400, "messages 必须是数组")
        return plan
    if action == "short-drama-character-reference-generate":
        _strict_object(value, {"project_id", "revision", "character_key"},
                       ("project_id", "revision", "character_key"))
        payload = {
            "project_id": _identifier(value["project_id"], "project_id"),
            "revision": _integer(value["revision"], "revision", 1, 2**63 - 1),
            "character_key": _identifier(value["character_key"], "character_key"),
        }
        return _plan(
            "generation:quote", "generation",
            generation_kind="short_drama_character_reference",
            endpoint="/api/gen/short-drama/generate-character-reference",
            payload=payload,
            quote_endpoint="/api/gen/short-drama/character-reference-quote",
            quote_body=payload,
        )
    if action == "short-drama-character-reference-confirm":
        required = {"project_id", "revision", "character_key", "reference_version"}
        _strict_object(value, required, tuple(required))
        return _plan(
            "short-drama:write", "proxy", base=CONTENT_BASE,
            path="/api/gen/short-drama/confirm-character-reference", method="POST",
            body={
                "project_id": _identifier(value["project_id"], "project_id"),
                "revision": _integer(value["revision"], "revision", 1, 2**63 - 1),
                "character_key": _identifier(value["character_key"], "character_key"),
                "reference_version": _integer(value["reference_version"], "reference_version", 1, 2**63 - 1),
            },
        )
    if action == "short-drama-preflight-plan":
        plan = _idempotent_proxy(
            "short-drama:write", "/api/gen/short-drama/preflight/generate",
            value, {"project_id", "conversation_revision", "quality_route"},
            ("project_id", "conversation_revision"),
        )
        plan["body"]["project_id"] = _identifier(plan["body"]["project_id"], "project_id")
        plan["body"]["conversation_revision"] = _integer(
            plan["body"]["conversation_revision"], "conversation_revision", 1, 2**63 - 1,
        )
        if "quality_route" in plan["body"]:
            plan["body"]["quality_route"] = _enum(
                plan["body"]["quality_route"], "quality_route", ("quick_draft", "quality_first"),
            )
        return plan
    if action == "short-drama-preflight-confirm":
        plan = _idempotent_proxy(
            "short-drama:write", "/api/gen/short-drama/preflight/confirm",
            value, {"project_id", "plan_id", "plan_version", "accepted_issue_keys"},
            ("project_id", "plan_id", "plan_version", "accepted_issue_keys"),
        )
        plan["body"]["project_id"] = _identifier(plan["body"]["project_id"], "project_id")
        plan["body"]["plan_id"] = _identifier(plan["body"]["plan_id"], "plan_id")
        plan["body"]["plan_version"] = _integer(
            plan["body"]["plan_version"], "plan_version", 1, 2**63 - 1,
        )
        issues = plan["body"]["accepted_issue_keys"]
        if not isinstance(issues, list) or len(issues) > 100:
            raise CLIAPIError(400, "accepted_issue_keys 必须是最多 100 项的数组")
        plan["body"]["accepted_issue_keys"] = [
            _identifier(item, "accepted_issue_keys") for item in issues
        ]
        return plan
    if action in {"short-drama-autodraft-preflight", "short-drama-autodraft-quote"}:
        allowed = {"project_id", "plan_id", "shot_key", "character_key", "avatar_id"}
        if action.endswith("preflight"):
            allowed.add("execution")
        _strict_object(value, allowed, ("project_id", "plan_id", "shot_key"))
        body = json.loads(json.dumps(value, ensure_ascii=False))
        for field in ("project_id", "plan_id", "shot_key"):
            body[field] = _identifier(body[field], field)
        path = (
            "/api/gen/short-drama/autodraft/provider-preflight"
            if action.endswith("preflight")
            else "/api/gen/short-drama/autodraft/provider-quote"
        )
        return _plan("short-drama:write" if action.endswith("preflight") else "short-drama:read", "proxy", base=CONTENT_BASE,
                     path=path, method="POST", body=body)
    if action == "short-drama-autodraft-start":
        plan = _idempotent_proxy(
            "short-drama:write", "/api/gen/short-drama/autodraft/provider-jobs",
            value, {"project_id", "quote_token"}, ("project_id", "quote_token"),
            extra_scopes=("generation:submit",),
        )
        plan["body"]["project_id"] = _identifier(plan["body"]["project_id"], "project_id")
        plan["body"]["quote_token"] = _string(plan["body"]["quote_token"], "quote_token", 1, 4096)
        return plan
    if action == "short-drama-autodraft-status":
        _strict_object(value, {"project_id", "job_id"}, ("project_id", "job_id"))
        project_id = urllib.parse.quote(_identifier(value["project_id"], "project_id"), safe="")
        job_id = urllib.parse.quote(_identifier(value["job_id"], "job_id"), safe="")
        return _plan("short-drama:read", "proxy", base=CONTENT_BASE,
                     path="/api/gen/short-drama/autodraft/provider-jobs/%s?project_id=%s" % (job_id, project_id))
    if action == "short-drama-delivery-quote":
        _strict_object(value, {"project_id", "version_id"}, ("project_id", "version_id"))
        return _plan("short-drama:read", "proxy", base=CONTENT_BASE,
                     path="/api/gen/short-drama/delivery/quote", method="POST",
                     body={"project_id": _identifier(value["project_id"], "project_id"),
                           "version_id": _identifier(value["version_id"], "version_id")})
    if action == "short-drama-delivery-start":
        plan = _idempotent_proxy(
            "short-drama:write", "/api/gen/short-drama/delivery/jobs",
            value, {"project_id", "quote_token"}, ("project_id", "quote_token"),
            extra_scopes=("generation:submit",),
        )
        plan["body"]["project_id"] = _identifier(plan["body"]["project_id"], "project_id")
        plan["body"]["quote_token"] = _string(plan["body"]["quote_token"], "quote_token", 1, 4096)
        return plan
    if action == "short-drama-delivery-status":
        _strict_object(value, {"project_id", "job_id"}, ("project_id", "job_id"))
        project_id = urllib.parse.quote(_identifier(value["project_id"], "project_id"), safe="")
        job_id = urllib.parse.quote(_identifier(value["job_id"], "job_id"), safe="")
        return _plan("short-drama:read", "proxy", base=CONTENT_BASE,
                     path="/api/gen/short-drama/delivery/jobs/%s?project_id=%s" % (job_id, project_id))
    if action in {"short-drama-completion-readiness", "short-drama-completion"}:
        _strict_object(value, {"project_id"}, ("project_id",))
        project_id = urllib.parse.quote(_identifier(value["project_id"], "project_id"), safe="")
        suffix = "/readiness" if action.endswith("readiness") else ""
        return _plan("short-drama:read", "proxy", base=CONTENT_BASE,
                     path="/api/gen/short-drama/completion%s?project_id=%s" % (suffix, project_id))
    if action == "short-drama-completion-confirm":
        allowed = {
            "project_id", "revision", "final_version_id", "asset_id",
            "delivery_hash", "acknowledged",
        }
        plan = _idempotent_proxy(
            "short-drama:write", "/api/gen/short-drama/completion/confirm",
            value, allowed, tuple(allowed),
        )
        body = plan["body"]
        body["project_id"] = _identifier(body["project_id"], "project_id")
        body["revision"] = _integer(body["revision"], "revision", 1, 2**63 - 1)
        body["final_version_id"] = _identifier(body["final_version_id"], "final_version_id")
        body["asset_id"] = _identifier(body["asset_id"], "asset_id")
        body["delivery_hash"] = _matched_string(
            body["delivery_hash"], "delivery_hash", _SHA256_RE, 64,
        )
        if body["acknowledged"] is not True:
            raise CLIAPIError(400, "acknowledged 必须为 true")
        return plan
    if action == "text-video-avatar-import":
        _strict_object(value, {"image_upload_id"}, ("image_upload_id",))
        return _plan(
            "assets:upload", "proxy", base=CONTENT_BASE,
            path="/api/gen/cli/text-video/avatar-import", method="POST",
            body={"image_upload_id": _upload_id(
                value["image_upload_id"], "image_upload_id")},
            internal=True,
        )
    if action == "text-video-plan":
        payload = _text_video_base_payload(value, ("ratio",))
        payload["ratio"] = float(Decimal(str(
            _number(value.get("ratio", 0.3), "ratio", 0.1, 0.5)
        )).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))
        return _plan(
            "generation:quote", "proxy", base=CONTENT_BASE,
            path="/api/gen/text-video/plan", method="POST", body=payload,
        )
    if action == "text-video-generate":
        payload = _text_video_payload(value)
        return _plan(
            "generation:quote", "generation",
            generation_kind="script_to_video",
            endpoint="/api/gen/script_to_video",
            payload=payload,
            quote_endpoint="/api/gen/text-video/quote",
            quote_body=payload,
            native_quote_token_field="quote_token",
            quote_result_fields=("scene_count", "cost_breakdown"),
        )
    if action == "matrix-template-generate":
        payload = _matrix_template_payload(value)
        return _plan(
            "generation:quote", "generation",
            generation_kind="matrix_template_video",
            endpoint="/api/gen/matrix-template", payload=payload,
        )
    if action == "matrix-template-batch-generate":
        payload = _matrix_template_batch_payload(value)
        return _plan(
            "generation:quote", "generation",
            generation_kind="matrix_template_video_batch",
            endpoint="/api/gen/matrix-template", payload=payload,
            quote_endpoint="/api/gen/cli/quote",
            quote_body={"kind": "matrix_template_video", "payload": payload["item"]},
            quote_multiplier=payload["count"],
            batch_item=payload["item"], batch_count=payload["count"],
        )
    if action in {"matrix-template-capability", "matrix-template-templates"}:
        _strict_object(value, set())
        suffix = action.removeprefix("matrix-template-")
        return _plan(
            "assets:read", "proxy", base=CONTENT_BASE,
            path="/api/gen/matrix-template/" + suffix,
        )
    if action in {
            "text-video-capability", "text-video-templates",
            "text-video-styles", "text-video-voices"}:
        _strict_object(value, set())
        suffix = action.removeprefix("text-video-")
        return _plan("assets:read", "proxy", base=CONTENT_BASE,
                     path="/api/gen/text-video/" + suffix)
    if action == "inspiration-catalog":
        _strict_object(value, set())
        return _plan("inspiration:read", "proxy", base=ADMIN_BASE,
                     path="/api/admin/public/inspirations")
    if action == "inspiration-likes":
        _strict_object(value, set())
        return _plan("inspiration:read", "proxy", base=CONTENT_BASE,
                     path="/api/gen/inspiration/likes")
    if action == "inspiration-like":
        _strict_object(value, {"id", "favorite"}, ("id", "favorite"))
        if not isinstance(value["favorite"], bool):
            raise CLIAPIError(400, "favorite 必须是布尔值")
        return _plan("inspiration:write", "proxy", base=CONTENT_BASE,
                     path="/api/gen/inspiration/like", method="POST",
                     body={"id": _integer(value["id"], "id", 1, 2**63 - 1),
                           "favorite": value["favorite"]})
    if action == "leads-crm":
        _strict_object(value, {"lead_ids"})
        lead_ids = value.get("lead_ids", [])
        if not isinstance(lead_ids, list) or len(lead_ids) > 100:
            raise CLIAPIError(400, "lead_ids 必须是最多 100 项的数组")
        lead_ids = [_matched_string(item, "lead_id", _LEAD_ID_RE, 40) for item in lead_ids]
        path = "/api/gen/leads/crm"
        if lead_ids:
            path += "?" + urllib.parse.urlencode({"ids": ",".join(dict.fromkeys(lead_ids))})
        return _plan("leads:read", "proxy", base=CONTENT_BASE, path=path)
    if action == "leads-crm-upsert":
        _strict_object(value, {"lead_id", "intent", "follow_status", "follow_note"}, ("lead_id",))
        body = {"lead_id": _matched_string(value["lead_id"], "lead_id", _LEAD_ID_RE, 40)}
        if "intent" in value:
            body["intent"] = _enum(value["intent"], "intent", ("高意向", "咨询", "价格敏感", "围观"))
        if "follow_status" in value:
            body["follow_status"] = _enum(
                value["follow_status"], "follow_status", ("待跟进", "跟进中", "已加微", "已成交", "无效"))
        if "follow_note" in value:
            body["follow_note"] = _string(value["follow_note"], "follow_note", 0, 300)
        return _plan("leads:write", "proxy", base=CONTENT_BASE,
                     path="/api/gen/leads/crm", method="POST", body=body)
    if action == "leads-delete":
        _strict_object(value, {"lead_ids"}, ("lead_ids",))
        lead_ids = value["lead_ids"]
        if not isinstance(lead_ids, list) or not 1 <= len(lead_ids) <= 100:
            raise CLIAPIError(400, "lead_ids 必须是 1-100 个线索标识")
        ids = []
        for lead_id in lead_ids:
            lead_id = _matched_string(lead_id, "lead_ids", _LEAD_ID_RE, 40)
            if lead_id not in ids:
                ids.append(lead_id)
        return _plan("leads:write", "proxy", base=CONTENT_BASE,
                     path="/api/gen/leads/crm", method="DELETE", body={"lead_ids": ids})
    if action in {"collect-content", "collect-video", "collect-transcript"}:
        _strict_object(value, {"url"}, ("url",))
        want = {
            "collect-content": "comments",
            "collect-video": "video",
            "collect-transcript": "transcript",
        }[action]
        return _plan(
            "generation:quote", "generation", generation_kind="collect",
            endpoint="/api/gen/collect", submit_base=LEADGEN_BASE,
            payload={"url": _collect_url(value["url"], allow_twitter=action == "collect-content"),
                     "want": [want]},
        )
    if action == "collect-search":
        _strict_object(value, {"platform", "keyword", "page"}, ("platform", "keyword"))
        return _plan(
            "generation:quote", "generation", generation_kind="collect_search",
            endpoint="/api/gen/collect_search", submit_base=LEADGEN_BASE,
            payload={
                "platform": _enum(value["platform"], "platform", ("douyin", "xhs")),
                "keyword": _string(value["keyword"], "keyword", 1, 120),
                "page": _integer(value.get("page", 1), "page", 1, 50),
            },
        )
    if action == "leads-generate":
        return _plan(
            "generation:quote", "generation", generation_kind="leads",
            endpoint="/api/gen/leads", submit_base=LEADGEN_BASE,
            payload=_leads_payload(value),
        )
    if action == "video-avatars":
        _strict_object(value, {"limit"})
        limit = _integer(value.get("limit", 120), "limit", 1, 120)
        return _plan("assets:read", "proxy", base=CONTENT_BASE,
                     path="/api/gen/video/avatars?" + urllib.parse.urlencode({"limit": limit}))
    if action == "audio-slots":
        _strict_object(value, set())
        return _plan("assets:read", "proxy", base=CONTENT_BASE,
                     path="/api/gen/audio/slots?include_points=0")
    if action == "voice-clone-status":
        _strict_object(value, {"slot_id"}, ("slot_id",))
        slot_id = _matched_string(
            value["slot_id"], "slot_id", _VOICE_SLOT_ID_RE, 88,
        )
        return _plan(
            "assets:read", "proxy", base=CONTENT_BASE,
            path="/api/gen/audio/clone-status?" + urllib.parse.urlencode({"slot_id": slot_id}),
        )
    if action == "voice-clone-create":
        _strict_object(value, {"slot_id", "name", "audio_upload_id"},
                       ("slot_id", "name", "audio_upload_id"))
        slot_id = _matched_string(
            value["slot_id"], "slot_id", _VOICE_SLOT_ID_RE, 88,
        )
        audio_id = _audio_upload_id(value["audio_upload_id"], "audio_upload_id")
        name = _string(value["name"], "name", 1, 40)
        # 名称会写入最终音色，也属于操作输入；相同三元组重放，不同名称可作为新操作恢复失败槽位。
        idempotency_key = "hqcli-" + hashlib.sha256(
            (slot_id + "\x00" + audio_id + "\x00" + name).encode("utf-8")).hexdigest()[:24]
        return _plan(
            "assets:write", "proxy", base=CONTENT_BASE,
            path="/api/gen/cli/voice-clone", method="POST", body={
                "slot_id": slot_id,
                "name": name,
                "audio_upload_id": audio_id,
            }, timeout=30, internal=True,
            headers={"Idempotency-Key": idempotency_key},
        )
    if action == "short-drama-projects":
        _strict_object(value, {"page", "page_size"})
        query = urllib.parse.urlencode({
            "page": _integer(value.get("page", 1), "page", 1, 100000),
            "page_size": _integer(value.get("page_size", 20), "page_size", 1, 50),
        })
        return _plan("short-drama:read", "proxy", base=CONTENT_BASE,
                     path="/api/gen/short-drama/projects?" + query)
    if action in {"short-drama-project", "short-drama-conversation", "short-drama-preflight"}:
        _strict_object(value, {"project_id"}, ("project_id",))
        project_id = urllib.parse.quote(_identifier(value["project_id"], "project_id"), safe="")
        suffix = action.removeprefix("short-drama-")
        if suffix == "project":
            path = "/api/gen/short-drama/project?id=" + project_id
        else:
            path = "/api/gen/short-drama/%s?project_id=%s" % (suffix, project_id)
        return _plan("short-drama:read", "proxy", base=CONTENT_BASE, path=path)
    if action == "short-drama-create":
        _strict_object(value, {
            "title", "synopsis", "ratio", "target_duration", "shot_count", "genre", "visual_style", "request_id",
        }, ("title", "synopsis", "ratio", "target_duration", "shot_count", "request_id"))
        target_duration = _integer(value["target_duration"], "target_duration", 1, 10**6)
        if target_duration not in (30, 45, 60):
            raise CLIAPIError(400, "短剧时长仅支持 30、45、60 秒")
        body = {
            "title": _string(value["title"], "title", 1, 80),
            "synopsis": _string(value["synopsis"], "synopsis", 8, 4000),
            "ratio": _enum(value["ratio"], "ratio", ("9:16", "16:9")),
            "target_duration": target_duration,
            "shot_count": _integer(value["shot_count"], "shot_count", 6, 10),
        }
        if "genre" in value:
            body["genre"] = _string(value["genre"], "genre", 0, 40)
        if "visual_style" in value:
            body["visual_style"] = _string(value["visual_style"], "visual_style", 1, 80)
        return _plan("short-drama:write", "proxy", base=CONTENT_BASE,
                     path="/api/gen/short-drama/projects", method="POST", body=body,
                     headers={"Idempotency-Key": _matched_string(
                         value["request_id"], "request_id", _IDEMPOTENCY_KEY_RE, 128)})
    if action == "short-drama-delete":
        _strict_object(value, {"project_id", "revision"}, ("project_id", "revision"))
        body = {
            "project_id": _identifier(value["project_id"], "project_id"),
            "revision": _integer(value["revision"], "revision", 1, 2**63 - 1),
        }
        return _plan("short-drama:write", "proxy", base=CONTENT_BASE,
                     path="/api/gen/short-drama/project/delete", method="POST", body=body)
    if action == "digital-ip-projects":
        _strict_object(value, set())
        return _plan("ip12:read", "proxy", base=CONTENT_BASE,
                     path="/api/gen/digital-ip/projects")
    if action in {"digital-ip-project", "digital-ip-report"}:
        _strict_object(value, {"project_id"}, ("project_id",))
        project_id = _identifier(value["project_id"], "project_id")
        suffix = "/report" if action == "digital-ip-report" else ""
        return _plan("ip12:read", "proxy", base=CONTENT_BASE,
                     path="/api/gen/digital-ip/projects/"
                     + urllib.parse.quote(project_id, safe="") + suffix)
    if action == "digital-ip-create":
        _strict_object(value, {"title"}, ("title",))
        return _plan("ip12:write", "proxy", base=CONTENT_BASE,
                     path="/api/gen/digital-ip/projects", method="POST",
                     body={"title": _string(value["title"], "title", 1, 80)})
    if action == "digital-ip-update":
        _strict_object(value, {"project_id", "revision", "title"}, ("project_id", "revision", "title"))
        project_id = _identifier(value["project_id"], "project_id")
        body = {"revision": _integer(value["revision"], "revision", 1, 2**63 - 1),
                "title": _string(value["title"], "title", 1, 80)}
        return _plan("ip12:write", "proxy", base=CONTENT_BASE,
                     path="/api/gen/digital-ip/projects/"
                     + urllib.parse.quote(project_id, safe=""),
                     method="PATCH", body=body)
    if action == "digital-ip-delete":
        _strict_object(value, {"project_id", "revision"}, ("project_id", "revision"))
        project_id = _identifier(value["project_id"], "project_id")
        body = {"revision": _integer(value["revision"], "revision", 1, 2**63 - 1)}
        return _plan("ip12:write", "proxy", base=CONTENT_BASE,
                     path="/api/gen/digital-ip/projects/"
                     + urllib.parse.quote(project_id, safe=""),
                     method="DELETE", body=body)
    if action == "ip12-projects":
        _strict_object(value, set())
        return _plan("ip12:read", "proxy", base=HERMES_BASE, path="/api/conversations")
    if action in {"ip12-project", "ip12-report"}:
        _strict_object(value, {"project_id"}, ("project_id",))
        project_id = _identifier(value["project_id"], "project_id")
        suffix = "/reports" if action == "ip12-report" else ""
        return _plan("ip12:read", "proxy", base=HERMES_BASE,
                     path="/api/conversations/" + urllib.parse.quote(project_id, safe="") + suffix)
    if action == "ip12-create":
        _strict_object(value, {"title"}, ("title",))
        title = _string(value["title"], "title", 1, 120)
        return _plan("ip12:write", "proxy", base=HERMES_BASE, path="/api/conversations",
                     method="POST", body={"title": title})
    if action == "ip12-delete":
        _strict_object(value, {"project_id"}, ("project_id",))
        project_id = _identifier(value["project_id"], "project_id")
        return _plan("ip12:write", "proxy", base=HERMES_BASE,
                     path="/api/conversations/" + urllib.parse.quote(project_id, safe=""),
                     method="DELETE")
    if action == "ip12-message":
        _strict_object(value, {"project_id", "message", "request_id"}, ("project_id", "message", "request_id"))
        project_id = _identifier(value["project_id"], "project_id")
        message = value["message"]
        if (not isinstance(message, str) or not 1 <= len(message.strip()) <= 4000
                or any(ord(ch) < 32 and ch not in "\r\n\t" for ch in message)):
            raise CLIAPIError(400, "message 长度或内容不合法")
        message = message.strip()
        request_id = _identifier(value["request_id"], "request_id")
        request_hash = _hash(json.dumps(
            {"project_id": project_id, "message": message}, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ))
        return _plan("ip12:chat", "proxy", base=HERMES_BASE, path="/api/chat-complete",
                     method="POST", body={"conversation_id": project_id, "message": message}, timeout=290,
                     headers={"Idempotency-Key": request_id}, request_id=request_id,
                     project_id=project_id, request_hash=request_hash)
    if action == "prompt-optimize":
        _strict_object(value, {"prompt", "kind"}, ("prompt", "kind"))
        prompt = _string(value["prompt"], "prompt", 1, 2000)
        kind = _enum(value["kind"], "kind", ("image", "video"))
        return _plan("prompt:optimize", "proxy", base=IMGGEN_BASE, path="/api/gen/reverse", method="POST",
                     body={"action": "optimize", "prompt": prompt, "kind": kind}, timeout=90)
    if action == "canvas-list":
        _strict_object(value, {"limit", "offset"})
        limit = _integer(value.get("limit", 20), "limit", 1, 100)
        offset = _integer(value.get("offset", 0), "offset", 0, 100000)
        return _plan("canvas:read", "canvas-list", limit=limit, offset=offset)
    if action == "canvas-get":
        _strict_object(value, {"board_id"}, ("board_id",))
        return _plan("canvas:read", "canvas-get", board_id=_identifier(value["board_id"], "board_id"))
    if action == "canvas-create":
        _strict_object(value, {"name", "prompt"}, ("name",))
        name = _string(value["name"], "name", 1, 48)
        prompt = _string(value.get("prompt", ""), "prompt", 0, 2000)
        nodes = []
        if prompt:
            nodes.append({"id": "n1", "type": "text", "x": 80, "y": 80, "collapsed": False,
                          "params": {"text": prompt}, "outputs": {"prompt": prompt}, "image": None,
                          "state": "", "note": ""})
        data = {"nid": len(nodes), "runLabel": "就绪", "zoom": 1,
                "scroll": {"left": 0, "top": 0}, "edges": [], "nodes": nodes}
        return _plan("canvas:write", "canvas-create", name=name, data=data)
    if action == "canvas-agent-plan":
        payload = _canvas_agent_payload(value)
        headers = {}
        if payload["scope"] == "collab":
            headers["X-Canvas-Board-Id"] = payload["project_id"].split(":", 1)[1]
        return _plan(
            "canvas:agent", "generation", generation_kind="canvas_agent",
            endpoint="/api/gen/canvas_agent", quote_endpoint="/api/gen/canvas-agent/quote",
            quote_body={}, payload=payload, quoted_cost_field="quoted_cost", submit_headers=headers,
        )
    if action == "canvas-ops":
        payload = _canvas_ops_payload(value)
        return _plan("canvas:edit", "canvas-ops", board_id=payload.pop("board_id"), payload=payload)
    if action == "canvas-delete":
        _strict_object(value, {"board_id"}, ("board_id",))
        return _plan("canvas:write", "canvas-delete",
                     board_id=_identifier(value["board_id"], "board_id"))
    if action == "tasks":
        _strict_object(value, {"days", "kind", "page", "page_size"})
        days = _integer(value.get("days", 30), "days", 1, 365)
        page = _integer(value.get("page", 1), "page", 1, 100000)
        page_size = _integer(value.get("page_size", 20), "page_size", 5, 50)
        kind = _string(value.get("kind", ""), "kind", 0, 32)
        if kind not in _TASK_KINDS:
            raise CLIAPIError(400, "kind 不是可查询的任务类型")
        query = urllib.parse.urlencode({"days": days, "kind": kind, "page": page, "page_size": page_size})
        return _plan("tasks:read", "proxy", base=CONTENT_BASE, path="/api/gen/points/history?" + query)
    if action == "task":
        _strict_object(value, {"job_id"}, ("job_id",))
        job_id = _integer(value["job_id"], "job_id", 1, 2**63 - 1)
        return _plan("tasks:read", "proxy", base=CONTENT_BASE, path="/api/gen/job/%d" % job_id)
    if action == "assets":
        _strict_object(value, {"kind", "limit", "offset"}, ("kind",))
        kind = _enum(value["kind"], "kind", ("image", "audio", "video", "copy", "collect", "leads", "breakdown"))
        limit = _integer(value.get("limit", 60), "limit", 1, 120)
        offset = _integer(value.get("offset", 0), "offset", 0, 100000)
        if kind == "image":
            path = "/api/gen/history?" + urllib.parse.urlencode({"kind": "image", "limit": limit, "offset": offset})
        elif kind in {"audio", "video"}:
            path = "/api/gen/%s/assets?" % kind + urllib.parse.urlencode({"limit": limit, "offset": offset})
        else:
            path = "/api/gen/assets?" + urllib.parse.urlencode({"kind": kind, "limit": limit, "offset": offset})
        return _plan("assets:read", "proxy", base=CONTENT_BASE, path=path)
    if action == "voices":
        _strict_object(value, set())
        return _plan("assets:read", "proxy", base=CONTENT_BASE, path="/api/gen/audio/voices")
    if action == "asset-favorite":
        _strict_object(value, {"kind", "key", "favorite"}, ("kind", "key", "favorite"))
        if not isinstance(value["favorite"], bool):
            raise CLIAPIError(400, "favorite 必须是布尔值")
        body = {
            "kind": _enum(value["kind"], "kind", ("image", "audio", "video", "avatar", "copy", "collect", "leads", "breakdown")),
            "key": _string(value["key"], "key", 1, 500), "favorite": value["favorite"],
        }
        return _plan("assets:write", "proxy", base=CONTENT_BASE, path="/api/gen/asset/favorite",
                     method="POST", body=body)
    if action == "asset-tags":
        _strict_object(value, {"kind", "key", "tags"}, ("kind", "key", "tags"))
        body = {
            "kind": _enum(value["kind"], "kind", ("image", "audio", "video", "avatar", "copy", "collect", "leads", "breakdown")),
            "key": _string(value["key"], "key", 1, 500), "tags": _tags(value["tags"]),
        }
        return _plan("assets:write", "proxy", base=CONTENT_BASE, path="/api/gen/asset/tags",
                     method="POST", body=body)
    if action == "asset-delete":
        _strict_object(value, {"kind", "id", "keys"}, ("kind",))
        kind = _enum(value["kind"], "kind", ("image", "audio", "video", "copy", "collect", "leads", "breakdown"))
        has_id = "id" in value
        has_keys = "keys" in value
        if has_id == has_keys:
            raise CLIAPIError(400, "asset-delete 必须且只能提供 id 或 keys 之一")
        if has_id:
            body = {"kind": kind, "id": _integer(value["id"], "id", 1, 2**63 - 1)}
            return _plan("assets:write", "proxy", base=CONTENT_BASE,
                         path="/api/gen/asset/delete", method="POST", body=body)
        keys = value["keys"]
        if not isinstance(keys, list) or not 1 <= len(keys) <= 200:
            raise CLIAPIError(400, "keys 必须是 1-200 个资产标识")
        ids = []
        for key in keys:
            key = _string(key, "keys", 1, 500)
            if key not in ids:
                ids.append(key)
        return _plan("assets:write", "proxy", base=CONTENT_BASE,
                     path="/api/gen/asset/batch-delete", method="POST", body={"kind": kind, "ids": ids})
    if action in {"video-compose-projects", "video-compose-project"}:
        allowed = {"project_id"} if action.endswith("project") else set()
        _strict_object(value, allowed, allowed)
        path = "/api/gen/video-compose/projects"
        if allowed:
            project_id = _matched_string(value["project_id"], "project_id", _VIDEO_COMPOSE_PROJECT_RE)
            path += "/" + project_id
        return _plan("video-compose:read", "proxy", base=CONTENT_BASE, path=path)
    if action == "video-compose-create":
        _strict_object(value, {"source_asset_id"}, ("source_asset_id",))
        body = {"source_asset_id": _integer(value["source_asset_id"], "source_asset_id", 1, 2**63 - 1)}
        return _plan("video-compose:write", "proxy", base=CONTENT_BASE,
                     path="/api/gen/video-compose/projects", method="POST", body=body)
    if action in {"video-compose-analyze", "video-compose-review", "video-compose-render"}:
        allowed = {"project_id", "expected_revision"}
        if action == "video-compose-review":
            allowed.add("decisions")
        _strict_object(value, allowed, tuple(allowed))
        project_id = _matched_string(value["project_id"], "project_id", _VIDEO_COMPOSE_PROJECT_RE)
        body = {"expected_revision": _integer(value["expected_revision"], "expected_revision", 1, 2**63 - 1)}
        suffix = {"video-compose-analyze": "analyze-source", "video-compose-review": "edit-decisions",
                  "video-compose-render": "render"}[action]
        if action == "video-compose-review":
            body["decisions"] = _video_compose_decisions(value["decisions"])
        return _plan("video-compose:write", "proxy", base=CONTENT_BASE,
                     path="/api/gen/video-compose/projects/%s/%s" % (project_id, suffix),
                     method="POST", body=body, timeout=300 if action != "video-compose-review" else 30)
    if action == "video-compose-delete":
        _strict_object(value, {"project_id", "expected_revision"}, ("project_id", "expected_revision"))
        project_id = _matched_string(value["project_id"], "project_id", _VIDEO_COMPOSE_PROJECT_RE)
        body = {"expected_revision": _integer(value["expected_revision"], "expected_revision", 1, 2**63 - 1)}
        return _plan("video-compose:write", "proxy", base=CONTENT_BASE,
                     path="/api/gen/video-compose/projects/%s" % project_id,
                     method="DELETE", body=body)
    if action == "digital-presenter-capability":
        _strict_object(value, set())
        return _plan("digital-presenter:read", "proxy", base=CONTENT_BASE,
                     path="/api/gen/digital-presenter/capability")
    if action == "digital-presenter-project":
        _strict_object(value, {"board_id", "project_id"}, ("board_id", "project_id"))
        board_id = _identifier(value["board_id"], "board_id")
        project_id = _matched_string(value["project_id"], "project_id", _DIGITAL_PRESENTER_PROJECT_RE)
        return _plan("digital-presenter:read", "proxy", base=CONTENT_BASE,
                     path="/api/gen/digital-presenter/project?id=" + urllib.parse.quote(project_id),
                     headers={"X-Canvas-Board-Id": board_id})
    if action in {"digital-presenter-create", "digital-presenter-update"}:
        control = {"board_id", "request_id"} if action.endswith("create") else {"board_id", "project_id", "revision"}
        editable = {"title", "script_text", "ratio", "resolution", "voice_key", "target_duration"}
        _strict_object(value, control | editable, tuple(control))
        board_id = _identifier(value["board_id"], "board_id")
        body = _digital_presenter_fields(value)
        headers = {"X-Canvas-Board-Id": board_id}
        if action == "digital-presenter-create":
            headers["Idempotency-Key"] = _matched_string(
                value["request_id"], "request_id", _IDEMPOTENCY_KEY_RE, 128)
            path, method = "/api/gen/digital-presenter/projects", "POST"
        else:
            if not body:
                raise CLIAPIError(400, "数字人口播更新至少需要一个字段")
            body.update({
                "project_id": _matched_string(value["project_id"], "project_id", _DIGITAL_PRESENTER_PROJECT_RE),
                "revision": _integer(value["revision"], "revision", 1, 2**63 - 1),
            })
            path, method = "/api/gen/digital-presenter/project", "PUT"
        return _plan("digital-presenter:write", "proxy", base=CONTENT_BASE,
                     path=path, method=method, body=body, headers=headers)
    if action == "digital-presenter-delete":
        _strict_object(value, {"board_id", "project_id", "revision"}, ("board_id", "project_id", "revision"))
        board_id = _identifier(value["board_id"], "board_id")
        project_id = _matched_string(value["project_id"], "project_id", _DIGITAL_PRESENTER_PROJECT_RE)
        revision = _integer(value["revision"], "revision", 1, 2**63 - 1)
        path = "/api/gen/digital-presenter/project?" + urllib.parse.urlencode(
            {"id": project_id, "revision": revision})
        return _plan("digital-presenter:write", "proxy", base=CONTENT_BASE,
                     path=path, method="DELETE", headers={"X-Canvas-Board-Id": board_id})
    if action in {
            "image-generate", "video-generate", "audio-generate",
            "video-lipsync",
            "digital-ip-text-generate", "digital-ip-batch-generate", "digital-ip-audio-generate",
            "cinematic-open-generate", "cinematic-motion-generate",
            "tryon-fast-generate", "tryon-classic-generate",
            "video-avatar-create"}:
        payload, generation_kind, endpoint = _generation_payload(action, value)
        return _plan("generation:quote", "generation", generation_kind=generation_kind,
                     endpoint=endpoint, payload=payload)
    raise CLIAPIError(404, "未知 CLI 能力", "unknown_action")


def _generation_payload(action, value):
    if action == "video-avatar-create":
        _strict_object(value, {"image_data", "name"}, ("image_data",))
        image_data = _string(value["image_data"], "image_data", 32, 12 * 1024 * 1024)
        if not image_data.startswith("data:image/"):
            raise CLIAPIError(400, "image_data 必须是图片 data URL（jpg/png/webp）")
        body = {"image_data": image_data}
        if "name" in value:
            body["name"] = _string(value["name"], "name", 1, 40)
        return body, "avatar", "/api/gen/avatar"
    if action == "video-lipsync":
        _strict_object(
            value,
            {"video_asset_id", "audio_asset_id", "quality", "dynamic_duration"},
            ("video_asset_id", "audio_asset_id"),
        )
        dynamic_duration = value.get("dynamic_duration", False)
        if not isinstance(dynamic_duration, bool):
            raise CLIAPIError(400, "dynamic_duration 必须是布尔值")
        return {
            "mode": "lipsync",
            "video_asset_id": _integer(
                value["video_asset_id"], "video_asset_id", 1, 2**63 - 1),
            "audio_asset_id": _integer(
                value["audio_asset_id"], "audio_asset_id", 1, 2**63 - 1),
            "lipsync_mode": _enum(
                value.get("quality", "speed"), "quality", ("speed", "precision")),
            "dynamic_duration": dynamic_duration,
        }, "video", "/api/gen/video"
    if action in {"digital-ip-text-generate", "digital-ip-audio-generate"}:
        mode = "text" if action == "digital-ip-text-generate" else "audio"
        required = ("text", "voice") if mode == "text" else ()
        _strict_object(value, {
            "avatar_id", "image_upload_id", "text", "voice", "audio_file", "audio_upload_id", "ratio", "motion",
            "subtitle", "subtitle_style", "subtitle_position",
        }, required)
        if bool(value.get("avatar_id")) == bool(value.get("image_upload_id")):
            raise CLIAPIError(400, "avatar_id 与 image_upload_id 必须且只能提供一个")
        if mode == "audio" and bool(value.get("audio_file")) == bool(value.get("audio_upload_id")):
            raise CLIAPIError(400, "audio_file 与 audio_upload_id 必须且只能提供一个")
        subtitle = value.get("subtitle", False)
        if not isinstance(subtitle, bool):
            raise CLIAPIError(400, "subtitle 必须是布尔值")
        body = {
            "mode": mode,
            "resolution": "1080p",
            "ratio": _enum(value.get("ratio", "9:16"), "ratio", ("9:16", "16:9", "1:1", "4:5", "5:4")),
            "motion": _enum(value.get("motion", "medium"), "motion", ("low", "medium", "high")),
            "subtitle": subtitle,
            "subtitle_style": _enum(value.get("subtitle_style", "white"), "subtitle_style", ("white", "variety", "bar")),
            "subtitle_position": _enum(value.get("subtitle_position", "bottom"), "subtitle_position", ("top", "upper", "center", "lower", "bottom")),
        }
        if value.get("avatar_id") is not None:
            body["avatar_id"] = _integer(value["avatar_id"], "avatar_id", 1, 2**63 - 1)
        else:
            body["image_upload_id"] = _upload_id(value["image_upload_id"], "image_upload_id")
        if mode == "text":
            body["text"] = _string(value["text"], "text", 1, 1000)
            body["voice"] = _string(value["voice"], "voice", 1, 128)
        else:
            if value.get("audio_file") is not None:
                body["audio_file"] = _string(value["audio_file"], "audio_file", 1, 500)
            else:
                body["audio_upload_id"] = _audio_upload_id(value["audio_upload_id"], "audio_upload_id")
        return body, "video", "/api/gen/video"
    if action == "digital-ip-batch-generate":
        _strict_object(value, {
            "avatars", "text", "voice", "ratio", "motion", "subtitle",
            "subtitle_style", "subtitle_position",
        }, ("avatars", "text", "voice"))
        items = value["avatars"]
        if not isinstance(items, list) or not 2 <= len(items) <= 5:
            raise CLIAPIError(400, "avatars 必须包含 2-5 项")
        avatars, seen = [], set()
        for index, item in enumerate(items, 1):
            _strict_object(item, {"avatar_id", "label"}, ("avatar_id",))
            avatar_id = _integer(item["avatar_id"], "avatar_id", 1, 2**63 - 1)
            if avatar_id in seen:
                raise CLIAPIError(400, "avatars 不能包含重复形象")
            seen.add(avatar_id)
            avatars.append({
                "avatar_id": avatar_id,
                "label": _string(item.get("label", "形象 %d" % index), "label", 1, 60),
            })
        single_input = {key: value[key] for key in (
            "text", "voice", "ratio", "motion", "subtitle", "subtitle_style",
            "subtitle_position",
        ) if key in value}
        single_input["avatar_id"] = avatars[0]["avatar_id"]
        body = _generation_payload("digital-ip-text-generate", single_input)[0]
        body.pop("avatar_id")
        body["avatars"] = avatars
        return body, "video_batch", "/api/gen/video/batch"
    if action == "cinematic-open-generate":
        _strict_object(value, {
            "avatar_id", "avatar_ids", "prompt", "ratio", "duration", "enhance_prompt",
            "reference_image_upload_ids", "reference_video_upload_ids",
        }, ("prompt",))
        if value.get("avatar_id") is not None and value.get("avatar_ids") is not None:
            raise CLIAPIError(400, "avatar_id 与 avatar_ids 只能选一个")
        raw_ids = value.get("avatar_ids")
        if raw_ids is None and value.get("avatar_id") is not None:
            raw_ids = [value["avatar_id"]]
        if not isinstance(raw_ids, list) or not 1 <= len(raw_ids) <= 3:
            raise CLIAPIError(400, "avatar_id 或 avatar_ids 必须提供 1-3 个形象")
        avatar_ids = []
        for item in raw_ids:
            clean = _integer(item, "avatar_ids", 1, 2**63 - 1)
            if clean in avatar_ids:
                raise CLIAPIError(400, "avatar_ids 不能重复")
            avatar_ids.append(clean)
        enhance_prompt = value.get("enhance_prompt", False)
        if not isinstance(enhance_prompt, bool):
            raise CLIAPIError(400, "enhance_prompt 必须是布尔值")
        body = {
            "cine_mode": "open",
            "avatar_ids": avatar_ids,
            "prompt": _string(value["prompt"], "prompt", 1, 2000),
            "resolution": "720p",
            "ratio": _enum(value.get("ratio", "9:16"), "ratio", ("9:16", "16:9", "1:1")),
            "duration": _integer(value.get("duration", 10), "duration", 4, 15),
            "enhance_prompt": enhance_prompt,
        }
        image_ids = value.get("reference_image_upload_ids")
        if image_ids is not None:
            image_limit = 9 - len(avatar_ids)
            if not isinstance(image_ids, list) or not 1 <= len(image_ids) <= image_limit:
                raise CLIAPIError(
                    400,
                    "reference_image_upload_ids 必须包含 1-%d 项（与形象共用 9 张额度）" % image_limit,
                )
            body["reference_image_upload_ids"] = [
                _upload_id(item, "reference_image_upload_ids") for item in image_ids
            ]
        video_ids = value.get("reference_video_upload_ids")
        if video_ids is not None:
            if not isinstance(video_ids, list) or not 1 <= len(video_ids) <= 3:
                raise CLIAPIError(400, "reference_video_upload_ids 必须包含 1-3 项")
            body["reference_video_upload_ids"] = [
                _video_upload_id(item, "reference_video_upload_ids") for item in video_ids
            ]
        return body, "cinematic", "/api/gen/cinematic"
    if action == "cinematic-motion-generate":
        _strict_object(value, {"avatar_id", "reference_video_upload_ids", "ratio"},
                       ("avatar_id", "reference_video_upload_ids"))
        references = value["reference_video_upload_ids"]
        if not isinstance(references, list) or len(references) != 1:
            raise CLIAPIError(400, "动作模仿需要且只接受 1 个参考视频")
        return {
            "cine_mode": "motion",
            "avatar_ids": [_integer(value["avatar_id"], "avatar_id", 1, 2**63 - 1)],
            "reference_video_upload_ids": [
                _video_upload_id(references[0], "reference_video_upload_ids")],
            "resolution": "720p",
            "ratio": _enum(value.get("ratio", "9:16"), "ratio", ("9:16", "16:9", "1:1")),
        }, "cinematic", "/api/gen/cinematic"
    if action == "tryon-fast-generate":
        _strict_object(value, {"person_image_upload_id", "clothes_upload_id", "seconds"},
                       ("person_image_upload_id", "clothes_upload_id"))
        return {
            "line": "2",
            "person_image_upload_id": _upload_id(value["person_image_upload_id"], "person_image_upload_id"),
            "clothes_upload_id": _upload_id(value["clothes_upload_id"], "clothes_upload_id"),
            "seconds": _integer(value.get("seconds", 6), "seconds", 5, 15),
        }, "tryon", "/api/gen/tryon"
    if action == "tryon-classic-generate":
        _strict_object(value, {
            "person_video_upload_id", "clothes_upload_id", "background_upload_id", "seconds",
        }, ("person_video_upload_id",))
        if not value.get("clothes_upload_id") and not value.get("background_upload_id"):
            raise CLIAPIError(400, "经典换装至少需要衣服图或背景图")
        body = {
            "line": "1",
            "person_video_upload_id": _video_upload_id(value["person_video_upload_id"], "person_video_upload_id"),
            "seconds": _integer(value.get("seconds", 6), "seconds", 1, 6),
        }
        for field in ("clothes_upload_id", "background_upload_id"):
            if value.get(field):
                body[field] = _upload_id(value[field], field)
        return body, "tryon", "/api/gen/tryon"
    if action == "image-generate":
        _strict_object(value, {
            "prompt", "provider", "ratio", "quality", "count", "variant",
            "model", "image_upload_id", "mask_upload_id", "reference_upload_ids",
        }, ("prompt",))
        provider = _enum(
            value.get("provider", "openai"), "provider",
            ("openai", "xiaole", "seedream", "banana"),
        )
        ratio = _string(value.get("ratio", "1:1"), "ratio", 1, 8)
        ratios = (
            ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")
            if provider == "banana" else ("1:1", "9:16", "16:9", "3:4")
        )
        if ratio not in ratios:
            raise CLIAPIError(400, "ratio 不支持该图片引擎")
        body = {
            "prompt": _string(value["prompt"], "prompt", 1, 2000),
            "provider": provider,
            "ratio": ratio,
            "quality": _enum(value.get("quality", "hd"), "quality", ("std", "hd")),
            "count": _integer(value.get("count", 1), "count", 1, 4),
            "source_page": "banana",
        }
        if provider == "banana":
            body["model"] = _enum(value.get("model", "nb2"), "model", ("nb2", "pro"))
            if body["count"] not in {1, 2, 4}:
                raise CLIAPIError(400, "Nano Banana count 仅支持 1、2、4")
        elif "model" in value:
            raise CLIAPIError(400, "model 仅用于 Nano Banana")
        if "variant" in value:
            if body["provider"] != "seedream":
                raise CLIAPIError(400, "variant 仅用于 seedream")
            body["variant"] = _enum(value["variant"], "variant", ("std", "pro"))
        if "image_upload_id" in value:
            body["image_upload_id"] = _upload_id(value["image_upload_id"], "image_upload_id")
        if "mask_upload_id" in value:
            body["mask_upload_id"] = _upload_id(value["mask_upload_id"], "mask_upload_id")
        if "reference_upload_ids" in value:
            references = value["reference_upload_ids"]
            limit = {"openai": 16, "seedream": 10, "xiaole": 4, "banana": 14}[body["provider"]]
            if not isinstance(references, list) or not 1 <= len(references) <= limit:
                raise CLIAPIError(400, "reference_upload_ids 必须包含 1-%d 项" % limit)
            body["reference_upload_ids"] = []
            for item in references:
                clean = _upload_id(item, "reference_upload_ids")
                if clean not in body["reference_upload_ids"]:
                    body["reference_upload_ids"].append(clean)
        if body.get("image_upload_id") and body.get("reference_upload_ids"):
            raise CLIAPIError(400, "单参考图和多参考图不能同时使用")
        if body.get("mask_upload_id") and not body.get("image_upload_id"):
            raise CLIAPIError(400, "蒙版必须同时提供 image_upload_id")
        if body.get("mask_upload_id") and body["provider"] != "openai":
            raise CLIAPIError(400, "蒙版局部修改仅支持 openai")
        if body.get("mask_upload_id") and body["count"] != 1:
            raise CLIAPIError(400, "蒙版局部修改 count 必须为 1")
        return body, "image", "/api/gen/image"
    if action == "video-generate":
        _strict_object(value, {
            "prompt", "channel", "ratio", "duration", "seconds", "resolution",
            "model", "generate_audio", "reference_upload_ids",
        }, ("prompt",))
        channel = _enum(value.get("channel", "grok"), "channel", ("grok", "micro", "omni", "minimax", "sora"))
        rule = _VIDEO_CHANNEL_RULES[channel]
        if channel == "sora":
            if "duration" in value or "generate_audio" in value:
                raise CLIAPIError(400, "Sora 使用 seconds，且不支持 generate_audio")
            model = _enum(value.get("model", rule["default_model"]), "model", tuple(rule["models"]))
            body = {
                "prompt": _string(value["prompt"], "prompt", 1, 2000),
                "channel": "sora",
                "model": model,
                "seconds": _integer(value.get("seconds", rule["default_seconds"]), "seconds", min(rule["seconds"]), max(rule["seconds"])),
                "ratio": _enum(value.get("ratio", rule["default_ratio"]), "ratio", tuple(rule["ratios"])),
                "resolution": _enum(value.get("resolution", rule["default_resolution"]), "resolution", tuple(rule["model_resolutions"][model])),
                "source_page": "video",
            }
            if body["seconds"] not in set(rule["seconds"]):
                raise CLIAPIError(400, "Sora seconds 仅支持 4、8、12")
            if "reference_upload_ids" in value:
                references = value["reference_upload_ids"]
                if not isinstance(references, list) or not 1 <= len(references) <= rule["reference_max"]:
                    raise CLIAPIError(400, "Sora reference_upload_ids 必须包含 1 项")
                body["reference_upload_ids"] = [_upload_id(references[0], "reference_upload_ids")]
            return body, "sora_video", "/api/gen/sora_video"
        if "seconds" in value:
            raise CLIAPIError(400, "seconds 仅用于 Sora")
        body = {
            "prompt": _string(value["prompt"], "prompt", 1, 2000),
            "channel": channel,
            "ratio": _enum(value.get("ratio", rule["default_ratio"]), "ratio", tuple(rule["ratios"])),
            "duration": _integer(value.get("duration", rule["default_duration"]), "duration", rule["duration"][0], rule["duration"][1]),
            "resolution": _enum(value.get("resolution", rule["default_resolution"]), "resolution", tuple(rule["resolutions"])),
            "source_page": "video",
        }
        selected_model = rule["default_model"]
        if "model" in value:
            if channel != "grok":
                raise CLIAPIError(400, "model 参数仅用于 grok")
            selected_model = _enum(value["model"], "model", tuple(rule["models"]))
            body["model"] = selected_model
        if "generate_audio" in value:
            if not isinstance(value["generate_audio"], bool) or channel != "micro":
                raise CLIAPIError(400, "generate_audio 仅用于 micro 且必须是布尔值")
            body["generate_audio"] = value["generate_audio"]
        if "reference_upload_ids" in value:
            references = value["reference_upload_ids"]
            limit = rule["reference_max"]
            if not isinstance(references, list) or not 1 <= len(references) <= limit:
                raise CLIAPIError(400, "reference_upload_ids 必须包含 1-%d 项" % limit)
            body["reference_upload_ids"] = []
            for item in references:
                clean = _upload_id(item, "reference_upload_ids")
                if clean not in body["reference_upload_ids"]:
                    body["reference_upload_ids"].append(clean)
        if channel == "grok":
            if selected_model in rule["reference_required_models"] and not body.get("reference_upload_ids"):
                raise CLIAPIError(400, "Grok Video 1.5 至少需要 1 张参考图")
            if body.get("reference_upload_ids") and body["resolution"] not in rule["reference_resolutions"]:
                raise CLIAPIError(400, "Grok 参考图生成仅支持 720p")
        return body, "xiaole_video", "/api/gen/xiaole_video"
    _strict_object(value, {"text", "voice", "speed", "pitch", "volume"}, ("text",))
    body = {"text": _string(value["text"], "text", 1, 1000), "source_page": "audio"}
    if "voice" in value:
        body["voice"] = _string(value["voice"], "voice", 1, 128)
    for field, default, minimum, maximum in (("pitch", 0, -12, 12), ("volume", 0, -50, 100)):
        body[field] = _integer(value.get(field, default), field, minimum, maximum)
    speed = value.get("speed", 1.0)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not 0.5 <= float(speed) <= 2:
        raise CLIAPIError(400, "speed 必须是 0.5-2 的数字")
    body["speed"] = round(float(speed), 1)
    return body, "audio", "/api/gen/audio"


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def issue_quote(secret, username, generation_kind, payload, cost, now=None, context=None):
    if not secret:
        raise CLIAPIError(503, "CLI 报价签名未配置", "not_configured")
    now = int(time.time() if now is None else now)
    cost = int(cost)
    if cost <= 0:
        raise CLIAPIError(502, "生成费用无效", "invalid_quote")
    claims = {
        "v": 1, "u": username, "k": generation_kind,
        "h": hashlib.sha256(_canonical(payload)).hexdigest(),
        "c": cost, "e": now + QUOTE_TTL, "n": secrets.token_hex(16),
    }
    if context is not None:
        if not isinstance(context, dict):
            raise CLIAPIError(500, "CLI 报价上下文无效", "invalid_quote_context")
        claims["x"] = context
    encoded = base64.urlsafe_b64encode(_canonical(claims)).decode("ascii").rstrip("=")
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return encoded + "." + signature, claims


def verify_quote(secret, token, username, generation_kind, payload, now=None):
    if not secret:
        raise CLIAPIError(503, "CLI 报价签名未配置", "not_configured")
    now = int(time.time() if now is None else now)
    try:
        encoded, signature = str(token or "").split(".", 1)
        expected = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except Exception:
        raise CLIAPIError(400, "报价凭证无效，请重新报价", "invalid_quote")
    payload_hash = hashlib.sha256(_canonical(payload)).hexdigest()
    if (claims.get("v") != 1 or claims.get("u") != username or claims.get("k") != generation_kind
            or claims.get("h") != payload_hash or not isinstance(claims.get("c"), int)
            or not isinstance(claims.get("e"), int) or not isinstance(claims.get("n"), str)):
        raise CLIAPIError(409, "报价与当前账号或参数不匹配，请重新报价", "quote_mismatch")
    if claims["e"] <= now:
        raise CLIAPIError(409, "报价已过期，请重新报价", "quote_expired")
    return claims
