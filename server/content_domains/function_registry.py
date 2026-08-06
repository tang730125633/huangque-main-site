"""Customer-visible function registry used by the operations console.

Feature flags remain the source of truth for accepting work.  This registry
only describes where a customer starts, which mode they chose, and which
runtime evidence must be joined back to that choice.
"""

from copy import deepcopy


def _endpoint(method, path):
    return {"method": method, "path": path}


VIDEO_FUNCTIONS = [
    {
        "key": "one_click",
        "name": "一键成片",
        "desc": "从已有视频资产开始，分析、确认粗剪并输出成片",
        "order": 10,
        "frontend_selector": 'a[href="one-click-video.html"]',
        "service": "content",
        "modes": [{
            "key": "video.one_click.compose",
            "name": "已有视频智能成片",
            "evidence_contract": {
                "acceptance_id_type": "project_id",
                "not_applicable": ["provider_task", "balance", "billing"],
            },
            "entrypoints": [
                _endpoint("POST", "/api/gen/video-compose/projects"),
                _endpoint("POST", "/api/gen/video-compose/projects/{project_id}/analyze-source"),
                _endpoint("POST", "/api/gen/video-compose/projects/{project_id}/edit-decisions"),
                _endpoint("POST", "/api/gen/video-compose/projects/{project_id}/render"),
                _endpoint("GET", "/api/gen/video-compose/projects/{project_id}/output"),
            ],
            "evidence_source": "video_compose_projects",
            "price_keys": [],
            "smoke_inputs": ["一条已完成的视频资产", "预设标题与字幕样式"],
        }],
    },
    {
        "key": "digital_ip",
        "name": "数字化 IP",
        "desc": "用人物形象和文案或现成音频生成数字人口播",
        "order": 20,
        "frontend_selector": '[data-function="talking"]',
        "service": "content",
        "flag_keys": ["video"],
        "dependencies": [
            {"key": "heygen", "role": "主生成", "requirement": "required"},
            {"key": "heygen_relay", "role": "传输兜底", "requirement": "optional",
             "condition": "仍需主生成 Key"},
            {"key": "cos", "role": "作品云存储", "requirement": "optional",
             "condition": "未配置时回退本地文件"},
        ],
        "modes": [
            {
                "key": "video.digital_ip.text.single",
                "name": "文案配音 · 单条",
                "entrypoints": [_endpoint("POST", "/api/gen/video")],
                "task_match": {"kind": "video", "mode": "text", "batch": False},
                "dependencies": [
                    {"key": "cosyvoice", "role": "默认与个人音色配音", "requirement": "required"},
                    {"key": "openai", "role": "兼容音色配音", "requirement": "optional",
                     "condition": "仅非 S_ / vip_ / cosyvoice- 音色使用"},
                ],
                "price_keys": ["video.talking.block"],
                "smoke_inputs": ["正脸人物图或已有形象", "短口播文案", "预设公共音色"],
            },
            {
                "key": "video.digital_ip.text.batch",
                "name": "文案配音 · 批量",
                "entrypoints": [_endpoint("POST", "/api/gen/video/batch")],
                "task_match": {"kind": "video", "mode": "text", "batch": True},
                "dependencies": [
                    {"key": "cosyvoice", "role": "默认与个人音色配音", "requirement": "required"},
                    {"key": "openai", "role": "兼容音色配音", "requirement": "optional",
                     "condition": "仅非 S_ / vip_ / cosyvoice- 音色使用"},
                ],
                "price_keys": ["video.talking.block"],
                "smoke_inputs": ["2 张正脸人物图", "短口播文案", "预设公共音色"],
            },
            {
                "key": "video.digital_ip.audio",
                "name": "现成音频生成",
                "entrypoints": [_endpoint("POST", "/api/gen/video")],
                "task_match": {"kind": "video", "mode": "audio"},
                "price_keys": ["video.talking.block"],
                "smoke_inputs": ["正脸人物图或已有形象", "短 MP3/WAV/M4A 音频"],
            },
        ],
    },
    {
        "key": "cinematic",
        "name": "电影化身",
        "desc": "选择自己的数字人形象进行动作模仿或开放式生成",
        "order": 30,
        "frontend_selector": '[data-function="cinematic"]',
        "service": "content",
        "flag_keys": ["cinematic"],
        "dependencies": [
            {"key": "heygen", "role": "主生成", "requirement": "required"},
            {"key": "cos", "role": "作品云存储", "requirement": "optional",
             "condition": "未配置时回退本地文件"},
        ],
        "shared_steps": [{
            "key": "video.cinematic.avatar",
            "name": "创建或选择形象",
            "flag_keys": ["avatar"],
            "entrypoints": [
                _endpoint("GET", "/api/gen/video/avatars"),
                _endpoint("POST", "/api/gen/avatar"),
            ],
            "task_match": {"kind": "avatar"},
            "dependencies": [
                {"key": "heygen", "role": "形象创建", "requirement": "required"},
                {"key": "cos", "role": "形象素材云存储", "requirement": "optional",
                 "condition": "未配置时回退本地文件"},
            ],
            "price_keys": ["avatar.create"],
        }],
        "modes": [
            {
                "key": "video.cinematic.motion",
                "name": "动作模仿",
                "frontend_selector": '[data-cine-mode="motion"]',
                "entrypoints": [_endpoint("POST", "/api/gen/cinematic")],
                "task_match": {"kind": "cinematic", "cine_mode": "motion"},
                "price_keys": ["video.cinematic.motion"],
                "smoke_inputs": ["1 个已就绪形象", "1 段短参考视频"],
            },
            {
                "key": "video.cinematic.open",
                "name": "开放式生成",
                "frontend_selector": '[data-cine-mode="open"]',
                "entrypoints": [_endpoint("POST", "/api/gen/cinematic")],
                "task_match": {"kind": "cinematic", "cine_mode": "open"},
                "price_keys": ["video.cinematic.open"],
                "smoke_inputs": ["1 个已就绪形象", "短提示词", "可选参考图或视频"],
            },
        ],
    },
    {
        "key": "tryon",
        "name": "换装换背景",
        "desc": "按客户选择的线路执行极速换装或经典换装换背景",
        "order": 40,
        "frontend_selector": '[data-function="tryon"]',
        "service": "content",
        "flag_keys": ["tryon"],
        "modes": [
            {
                "key": "video.tryon.fast",
                "name": "线路二 · 极速",
                "frontend_selector": '[data-line="2"]',
                "entrypoints": [_endpoint("POST", "/api/gen/tryon")],
                "task_match": {"kind": "tryon", "line": "2"},
                "dependencies": [
                    {"key": "wavespeed", "role": "主生成", "requirement": "required"},
                    {"key": "cos", "role": "生成素材传输", "requirement": "required"},
                ],
                "price_keys": ["video.tryon.single"],
                "smoke_inputs": ["人物照片", "衣服图"],
            },
            {
                "key": "video.tryon.classic",
                "name": "线路一 · 经典",
                "frontend_selector": '[data-line="1"]',
                "entrypoints": [_endpoint("POST", "/api/gen/tryon")],
                "task_match": {"kind": "tryon", "line": "1"},
                "dependencies": [
                    {"key": "runninghub", "role": "主生成", "requirement": "required"},
                    {"key": "cos", "role": "作品云存储", "requirement": "optional",
                     "condition": "未配置时回退本地文件"},
                ],
                "price_keys": ["video.tryon.single", "video.tryon.double"],
                "smoke_inputs": ["人物视频", "衣服图或背景图"],
            },
        ],
    },
    {
        "key": "grok",
        "name": "果肉视频生成",
        "desc": "输入提示词，选择是否提供参考图后生成视频",
        "order": 50,
        "frontend_selector": '[data-function="grok"]',
        "service": "content",
        "flag_keys": ["grok_video"],
        "alternative_selections": {
            "grok_provider": {"env": "GROK_VIDEO_PROVIDER", "default": "xai"},
        },
        "dependencies": [
            {"key": "xai", "role": "生成线路", "requirement": "alternative",
             "alternative_group": "grok_provider", "selection_value": "xai",
             "condition": "服务器选择 xai 时使用"},
            {"key": "xiaolevideo", "role": "生成线路", "requirement": "alternative",
             "alternative_group": "grok_provider", "selection_value": "xiaole",
             "condition": "服务器选择 xiaole 时使用"},
            {"key": "cos", "role": "参考图与作品云存储", "requirement": "optional",
             "condition": "普通生成可回退本地；关键帧公网转存需配置"},
        ],
        "modes": [
            {
                "key": "video.grok.text",
                "name": "文生视频",
                "entrypoints": [_endpoint("POST", "/api/gen/xiaole_video")],
                "task_match": {"kind": "xiaole_video", "channel": "grok", "operation": "generate", "reference_count": 0},
                "price_keys": ["video.grok.v1.480p", "video.grok.v1.720p"],
                "smoke_inputs": ["短提示词"],
            },
            {
                "key": "video.grok.image",
                "name": "图生视频",
                "entrypoints": [_endpoint("POST", "/api/gen/xiaole_video")],
                "task_match": {"kind": "xiaole_video", "channel": "grok", "operation": "generate", "reference_count": ">0"},
                "dependencies": [
                    {"key": "cos", "role": "xAI 本地参考图上传", "requirement": "required",
                     "when": {"alternative_group": "grok_provider", "equals": "xai"}},
                ],
                "price_keys": ["video.grok.v1.720p", "video.grok.v1_5.720p"],
                "smoke_inputs": ["短提示词", "1 张低成本参考图"],
            },
        ],
    },
    {
        "key": "sora",
        "name": "Sora 2",
        "desc": "非真人通用视频生成；页签常显，接单能力单独判断",
        "order": 60,
        "frontend_selector": '[data-function="sora"]',
        "service": "content",
        "flag_keys": ["sora_video"],
        "acceptance_health_key": "sora_video_enabled",
        "dependencies": [
            {"key": "openai", "role": "主生成", "requirement": "required"},
            {"key": "cos", "role": "作品云存储", "requirement": "optional",
             "condition": "未配置时回退本地文件"},
        ],
        "modes": [
            {
                "key": "video.sora.text",
                "name": "文生视频",
                "entrypoints": [_endpoint("POST", "/api/gen/sora_video")],
                "task_match": {"kind": "sora_video", "reference_count": 0},
                "price_keys": [
                    "video.sora.standard.720p", "video.sora.pro.720p",
                    "video.sora.pro.1024p", "video.sora.pro.1080p",
                ],
                "smoke_inputs": ["短提示词"],
            },
            {
                "key": "video.sora.image",
                "name": "首帧图生视频",
                "entrypoints": [_endpoint("POST", "/api/gen/sora_video")],
                "task_match": {"kind": "sora_video", "reference_count": ">0"},
                "price_keys": [
                    "video.sora.standard.720p", "video.sora.pro.720p",
                    "video.sora.pro.1024p", "video.sora.pro.1080p",
                ],
                "smoke_inputs": ["短提示词", "1 张非真人参考图"],
            },
        ],
    },
    {
        "key": "minimax",
        "name": "麦克视频",
        "desc": "用人物参考图生成身份稳定的真人剧情短片",
        "order": 70,
        "frontend_selector": '[data-function="minimax"]',
        "service": "content",
        "flag_keys": ["minimax_h3_video"],
        "surface_visibility_key": "minimax_h3_video_enabled",
        "dependencies": [
            {"key": "minimax", "role": "主生成", "requirement": "required"},
            {"key": "cos", "role": "作品云存储", "requirement": "optional",
             "condition": "未配置时回退本地文件"},
        ],
        "modes": [{
            "key": "video.minimax.image",
            "name": "多参考图生视频",
            "entrypoints": [_endpoint("POST", "/api/gen/xiaole_video")],
            "task_match": {"kind": "xiaole_video", "channel": "minimax"},
            "price_keys": ["video.minimax_h3.768p"],
            "smoke_inputs": ["短提示词", "1 张人物参考图"],
        }],
    },
    {
        "key": "omni",
        "name": "Omni 视频",
        "desc": "支持文生、图生与多参考图生成",
        "order": 80,
        "frontend_selector": '[data-function="omni"]',
        "service": "content",
        "flag_keys": ["omni_video"],
        "surface_visibility_key": "omni_video_enabled",
        "dependencies": [
            {"key": "gemini", "role": "主生成", "requirement": "required"},
            {"key": "cos", "role": "作品云存储", "requirement": "optional",
             "condition": "未配置时回退本地文件"},
        ],
        "modes": [
            {
                "key": "video.omni.text",
                "name": "文生视频",
                "entrypoints": [_endpoint("POST", "/api/gen/xiaole_video")],
                "task_match": {"kind": "xiaole_video", "channel": "omni", "reference_count": 0},
                "price_keys": ["video.omni"],
                "smoke_inputs": ["短提示词"],
            },
            {
                "key": "video.omni.image",
                "name": "图生 / 多参考图",
                "entrypoints": [_endpoint("POST", "/api/gen/xiaole_video")],
                "task_match": {"kind": "xiaole_video", "channel": "omni", "reference_count": ">0"},
                "price_keys": ["video.omni"],
                "smoke_inputs": ["短提示词", "1 张低成本参考图"],
            },
        ],
    },
    {
        "key": "seedance",
        "name": "Seedance 视频",
        "desc": "支持文生、图生；选择 AI 超清时增加一段超清链路",
        "order": 90,
        "frontend_selector": '[data-function="micro"]',
        "service": "content",
        "flag_keys": ["seedance_video"],
        "surface_visibility_key": "seedance_video_enabled",
        "dependencies": [
            {"key": "seedance", "role": "主生成", "requirement": "required"},
            {"key": "wavespeed", "role": "条件超清", "requirement": "optional",
             "condition": "勾选 AI 超清 1080p"},
            {"key": "cos", "role": "参考图与作品云存储", "requirement": "optional",
             "condition": "文生可回退本地；参考图上传与超清需配置"},
        ],
        "modes": [
            {
                "key": "video.seedance.text",
                "name": "文生视频",
                "entrypoints": [_endpoint("POST", "/api/gen/xiaole_video")],
                "task_match": {"kind": "xiaole_video", "channel": "micro", "reference_count": 0},
                "price_keys": ["video.seedance"],
                "smoke_inputs": ["短提示词"],
            },
            {
                "key": "video.seedance.image",
                "name": "图生 / 多参考图",
                "entrypoints": [_endpoint("POST", "/api/gen/xiaole_video")],
                "task_match": {"kind": "xiaole_video", "channel": "micro", "reference_count": ">0"},
                "dependencies": [
                    {"key": "cos", "role": "本地参考图上传", "requirement": "required"},
                ],
                "price_keys": ["video.seedance"],
                "smoke_inputs": ["短提示词", "1 张低成本参考图"],
            },
        ],
    },
]


_PAGE_DEFS = [
    ("inspiration", "灵感设计", "/workbench/inspiration.html"),
    ("leads", "平台获客", "/workbench/leads.html"),
    ("collect", "内容爬取", "/workbench/collect.html"),
    ("banana", "图片生成", "/workbench/banana.html"),
    ("video", "视频生成", "/workbench/video.html"),
    ("audio", "音频生成", "/workbench/audio.html"),
    ("script", "文案编导", "/workbench/script.html"),
    ("short-drama", "短剧创作", "/workbench/short-drama.html"),
    ("canvas", "无限画布", "/workbench/canvas.html"),
    ("assets", "我的资产", "/workbench/assets.html"),
    ("pricing", "点数价格", "/workbench/pricing.html"),
    ("invite", "邀请中心", "/workbench/invite.html"),
    ("tutorials", "教程视频", "/workbench/tutorials.html"),
    ("settings", "通用设置", "/workbench/settings.html"),
]

FUNCTION_REGISTRY = [
    {
        "key": key,
        "name": name,
        "path": path,
        "order": order,
        "inventory_status": "verified" if key == "video" else "pending",
        "functions": VIDEO_FUNCTIONS if key == "video" else [],
        "auxiliary_actions": ([{
            "key": "video.asset.import_h3",
            "name": "导入 H3 成片",
            "entrypoint": _endpoint("POST", "/api/gen/video/import"),
            "scope": "资产导入，不计入生成渠道健康率",
        }] if key == "video" else []),
    }
    for order, (key, name, path) in enumerate(_PAGE_DEFS)
]


def list_pages():
    return deepcopy(FUNCTION_REGISTRY)


def _task_rules():
    rules = []
    for page in FUNCTION_REGISTRY:
        for feature in page["functions"]:
            for leaf in feature.get("shared_steps", []) + feature.get("modes", []):
                if leaf.get("task_match"):
                    rules.append((leaf["key"], leaf["task_match"]))
    return rules


TASK_RULES = _task_rules()


def _matches(actual, expected):
    for key, value in expected.items():
        found = actual.get(key)
        if value == ">0":
            if not isinstance(found, int) or found <= 0:
                return False
        elif found != value:
            return False
    return True


def classify_task(kind, metadata=None):
    """Return the registry leaf matched by a durable job row."""
    metadata = metadata or {}
    kind = str(kind or "").strip().lower()
    if metadata.get("source_page") not in {None, "", "video"}:
        return None
    try:
        references = int(metadata.get("reference_count") or 0)
    except (TypeError, ValueError):
        references = 0
    actual = {
        "kind": kind,
        "mode": str(metadata.get("mode") or ("text" if kind == "video" else "")).strip().lower(),
        "cine_mode": str(metadata.get("cine_mode") or "").strip().lower(),
        "line": str(metadata.get("line") or "").strip(),
        "channel": str(metadata.get("channel") or ("grok" if kind == "xiaole_video" else "")).strip().lower(),
        "operation": str(metadata.get("operation") or ("generate" if kind == "xiaole_video" else "")).strip().lower(),
        "reference_count": references,
        "batch": bool(metadata.get("batch") or metadata.get("batch_id")),
    }
    return next((key for key, rule in TASK_RULES if _matches(actual, rule)), None)


def validate_registry():
    page_keys = [page["key"] for page in FUNCTION_REGISTRY]
    if len(page_keys) != len(set(page_keys)):
        raise ValueError("duplicate customer page key")
    operation_keys = []
    for page in FUNCTION_REGISTRY:
        for feature in page["functions"]:
            leaves = feature.get("shared_steps", []) + feature.get("modes", [])
            operation_keys.extend(leaf["key"] for leaf in leaves)
            for leaf in leaves:
                for dependency in feature.get("dependencies", []) + leaf.get("dependencies", []):
                    requirement = dependency.get("requirement")
                    if requirement not in {"required", "optional", "alternative"}:
                        raise ValueError("invalid dependency requirement")
                    if requirement == "alternative" and not dependency.get("alternative_group"):
                        raise ValueError("alternative dependency needs a group")
    if len(operation_keys) != len(set(operation_keys)):
        raise ValueError("duplicate operation key")
    return True


validate_registry()
