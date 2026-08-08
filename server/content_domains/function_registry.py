"""Customer-visible function registry used by the operations console.

Feature flags remain the source of truth for accepting work.  This registry
only describes where a customer starts, which mode they chose, and which
runtime evidence must be joined back to that choice.
"""

from copy import deepcopy


def _endpoint(method, path):
    return {"method": method, "path": path}


QA_FACE_IMAGE = "@fixture/zelong-portrait.jpg"
QA_FULL_BODY_IMAGE = "@fixture/zelong-full-body.jpg"
QA_OUTFIT_IMAGE = "@fixture/tryon-outfit.jpg"
QA_BACKGROUND_IMAGE = "@fixture/tryon-background.jpg"
QA_VOICE_AUDIO = "@fixture/zelong-voice-5s.mp3"
QA_PRODUCT_IMAGE = "@fixture/qa-serum.png"
QA_MOTION_VIDEO = "@fixture/zelong-motion.mp4"
QA_PROMPT = "琥珀色精华瓶置于石台上，柔和晨光缓慢扫过瓶身，镜头平稳推进，无人物、无文字、无标识"
QA_IMAGE_PROMPT = "琥珀色精华瓶置于浅灰石台中央，柔和晨光，干净电商摄影，无人物、无文字、无标识"
QA_IMAGE_EDIT_PROMPT = "仅将瓶身左侧背景改为柔和浅金色，保持产品主体、构图和光线不变，无文字、无标识"
QA_AUDIO_TEXT = "你好，这是黄雀音频功能的自动质检。现在正在验证生成、播放、下载和点数记录。"


def _validation(prefill=None, manual_requirements=None, supported=True, blocked_reason=""):
    """Private server-side fixture contract; list_pages() strips its inputs."""
    return {
        "prefill": deepcopy(prefill or {}),
        "manual_requirements": list(manual_requirements or []),
        "supported": bool(supported),
        "blocked_reason": str(blocked_reason or ""),
    }


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
                "not_applicable": ["provider_task", "billing"],
            },
            "entrypoints": [
                _endpoint("POST", "/api/gen/video-compose/projects"),
                _endpoint("GET", "/api/gen/video-compose/projects/{project_id}"),
                _endpoint("POST", "/api/gen/video-compose/projects/{project_id}/analyze-source"),
                _endpoint("POST", "/api/gen/video-compose/projects/{project_id}/edit-decisions"),
                _endpoint("POST", "/api/gen/video-compose/projects/{project_id}/render"),
                _endpoint("GET", "/api/gen/video-compose/projects/{project_id}/output"),
            ],
            "evidence_source": "video_compose_projects",
            "validation": _validation(
                manual_requirements=["从测试账号的视频资产选择一条短视频"],
                supported=False,
                blocked_reason="专用测试账号尚未登记可重复使用的成片资产",
            ),
            "dependencies": [{
                "key": "openai", "role": "源视频语音识别", "requirement": "required",
                "credential_source": "env", "condition": "分析源视频时调用同步 ASR",
            }],
            "evidence_gaps": ["分析失败与用户中途退出尚未形成可区分的阶段证据"],
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
        "evidence_gaps": ["配音、HeyGen 与后处理仍共用一条任务证据，子步骤尚未拆开"],
        "modes": [
            {
                "key": "video.digital_ip.text.single",
                "name": "文案配音 · 单条",
                "entrypoints": [_endpoint("POST", "/api/gen/video")],
                "task_match": {"kind": "video", "mode": "text", "batch": False},
                "dependencies": [
                    {"key": "cosyvoice", "role": "默认与个人音色配音", "requirement": "required"},
                    {"key": "openai", "role": "兼容音色配音", "requirement": "optional",
                     "credential_source": "env",
                     "condition": "仅非 S_ / vip_ / cosyvoice- 音色使用"},
                ],
                "price_keys": ["video.talking.block"],
                "smoke_inputs": ["正脸人物图或已有形象", "短口播文案", "预设公共音色"],
                "validation": _validation({
                    "mode": "text", "image_url": QA_FACE_IMAGE,
                    "prompt": "大家好，这是黄雀视频功能的完整链路验收。",
                }),
            },
            {
                "key": "video.digital_ip.text.batch",
                "name": "文案配音 · 批量",
                "entrypoints": [_endpoint("POST", "/api/gen/video/batch")],
                "task_match": {"kind": "video", "mode": "text", "batch": True},
                "dependencies": [
                    {"key": "cosyvoice", "role": "默认与个人音色配音", "requirement": "required"},
                    {"key": "openai", "role": "兼容音色配音", "requirement": "optional",
                     "credential_source": "env",
                     "condition": "仅非 S_ / vip_ / cosyvoice- 音色使用"},
                ],
                "price_keys": ["video.talking.block"],
                "smoke_inputs": ["2 张正脸人物图", "短口播文案", "预设公共音色"],
                "validation": _validation({
                    "mode": "text", "batch": True,
                    "reference_images": [QA_FACE_IMAGE, QA_FACE_IMAGE],
                    "prompt": "大家好，这是黄雀批量口播链路验收。",
                }, supported=False, blocked_reason="批量入口尚未接入幂等提交保护"),
            },
            {
                "key": "video.digital_ip.audio",
                "name": "现成音频生成",
                "entrypoints": [_endpoint("POST", "/api/gen/video")],
                "task_match": {"kind": "video", "mode": "audio"},
                "price_keys": ["video.talking.block"],
                "smoke_inputs": ["正脸人物图或已有形象", "短 MP3/WAV/M4A 音频"],
                "validation": _validation(
                    {"mode": "audio", "image_url": QA_FACE_IMAGE,
                     "audio_url": QA_VOICE_AUDIO},
                ),
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
                "validation": _validation(
                    {"mode": "cinematic", "cine_mode": "motion",
                     "reference_video_url": QA_MOTION_VIDEO},
                    ["测试账号至少有 1 个已就绪电影化身形象"],
                ),
            },
            {
                "key": "video.cinematic.open",
                "name": "开放式生成",
                "frontend_selector": '[data-cine-mode="open"]',
                "entrypoints": [_endpoint("POST", "/api/gen/cinematic")],
                "task_match": {"kind": "cinematic", "cine_mode": "open"},
                "price_keys": ["video.cinematic.open"],
                "smoke_inputs": ["1 个已就绪形象", "短提示词", "可选参考图或视频"],
                "validation": _validation(
                    {"mode": "cinematic", "cine_mode": "open",
                     "prompt": "人物在明亮工作室自然面向镜头，轻轻点头并挥手，固定机位，无文字无标识"},
                    ["测试账号至少有 1 个已就绪电影化身形象"],
                ),
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
                "evidence_gaps": ["WaveSpeed prediction_id 尚未持久化，无法证明供应商已接单"],
                "price_keys": ["video.tryon.single"],
                "smoke_inputs": ["人物照片", "衣服图"],
                "validation": _validation(
                    {"mode": "tryon", "reference_video_url": QA_FULL_BODY_IMAGE,
                     "image_url": QA_OUTFIT_IMAGE},
                ),
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
                "evidence_gaps": ["换装与换背景可能产生两个任务，当前尚未分别持久化 task_id"],
                "price_keys": ["video.tryon.single", "video.tryon.double"],
                "smoke_inputs": ["人物视频", "衣服图或背景图"],
                "validation": _validation(
                    {"mode": "tryon", "reference_video_url": QA_MOTION_VIDEO,
                     "image_url": QA_OUTFIT_IMAGE,
                     "background_url": QA_BACKGROUND_IMAGE},
                ),
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
             "credential_source": "pool",
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
                "validation": _validation({
                    "mode": "grok", "prompt": QA_PROMPT,
                    "duration": 5, "resolution": "480p", "ratio": "9:16",
                }),
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
                "validation": _validation({
                    "mode": "grok", "prompt": QA_PROMPT,
                    "duration": 5, "resolution": "720p", "ratio": "9:16",
                    "reference_images": [QA_PRODUCT_IMAGE],
                }),
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
            {"key": "openai", "role": "主生成", "requirement": "required",
             "credential_source": "pool"},
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
                "validation": _validation({
                    "mode": "sora", "prompt": QA_PROMPT,
                    "duration": 4, "resolution": "720p", "ratio": "9:16",
                }),
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
                "validation": _validation({
                    "mode": "sora", "prompt": QA_PROMPT,
                    "duration": 4, "resolution": "720p", "ratio": "9:16",
                    "reference_images": [QA_PRODUCT_IMAGE],
                }),
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
            {"key": "minimax", "role": "主生成", "requirement": "required",
             "credential_source": "pool"},
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
            "validation": _validation({
                "mode": "minimax",
                "prompt": "@图片1仅作为人物身份参考。人物在明亮工作室自然看向镜头并微笑，无文字无标识。",
                "duration": 5, "ratio": "9:16",
                "reference_images": [QA_FACE_IMAGE],
            }),
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
            {"key": "gemini", "role": "主生成", "requirement": "required",
             "credential_source": "pool"},
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
                "validation": _validation({
                    "mode": "omni", "prompt": QA_PROMPT,
                    "duration": 4, "ratio": "9:16",
                }),
            },
            {
                "key": "video.omni.image",
                "name": "图生 / 多参考图",
                "entrypoints": [_endpoint("POST", "/api/gen/xiaole_video")],
                "task_match": {"kind": "xiaole_video", "channel": "omni", "reference_count": ">0"},
                "price_keys": ["video.omni"],
                "smoke_inputs": ["短提示词", "1 张低成本参考图"],
                "validation": _validation({
                    "mode": "omni", "prompt": QA_PROMPT,
                    "duration": 4, "ratio": "9:16",
                    "reference_images": [QA_PRODUCT_IMAGE],
                }),
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
            {"key": "seedance", "role": "主生成", "requirement": "required",
             "credential_source": "pool"},
            {"key": "wavespeed", "role": "条件超清", "requirement": "optional",
             "condition": "勾选 AI 超清 1080p"},
            {"key": "cos", "role": "参考图与作品云存储", "requirement": "optional",
             "condition": "文生可回退本地；参考图上传与超清需配置"},
        ],
        "evidence_gaps": ["勾选 AI 超清时的第二段 WaveSpeed 任务尚未在调用链中单独展示"],
        "modes": [
            {
                "key": "video.seedance.text",
                "name": "文生视频",
                "entrypoints": [_endpoint("POST", "/api/gen/xiaole_video")],
                "task_match": {"kind": "xiaole_video", "channel": "micro", "reference_count": 0},
                "price_keys": ["video.seedance"],
                "smoke_inputs": ["短提示词"],
                "validation": _validation({
                    "mode": "micro", "prompt": QA_PROMPT,
                    "duration": 4, "resolution": "480p", "ratio": "9:16",
                    "generate_audio": False,
                }),
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
                "validation": _validation({
                    "mode": "micro", "prompt": QA_PROMPT,
                    "duration": 4, "resolution": "480p", "ratio": "9:16",
                    "generate_audio": False,
                    "reference_images": [QA_PRODUCT_IMAGE],
                }),
            },
        ],
    },
]


def _image_validation(reference=False, inpaint=False):
    prefill = {
        "prompt": QA_IMAGE_EDIT_PROMPT if inpaint else QA_IMAGE_PROMPT,
        "ratio": "1:1", "quality": "std", "count": 1,
    }
    if inpaint:
        prefill.update({"image_url": QA_PRODUCT_IMAGE, "mask_url": QA_PRODUCT_IMAGE})
    elif reference:
        prefill["reference_images"] = [QA_PRODUCT_IMAGE]
    return _validation(prefill)


IMAGE_FUNCTIONS = [
    {
        "key": "banana",
        "name": "纳米香蕉",
        "desc": "选择纳米香蕉 2 或 Pro，支持文生图与多图参考生成",
        "order": 10,
        "frontend_selector": '[data-engine="banana"]',
        "service": "imggen",
        "flag_keys": ["banana"],
        "dependencies": [
            {"key": "gemini", "role": "主生成", "requirement": "required", "credential_source": "env"},
        ],
        "modes": [
            {
                "key": "image.banana.nb2.text",
                "name": "纳米香蕉 2 · 文生图",
                "frontend_selector": '[data-variant="nb2"]',
                "entrypoints": [_endpoint("POST", "/api/gen/banana")],
                "task_match": {"kind": "image", "source_page": "banana", "provider": "banana", "model": "nb2", "reference_count": 0},
                "evidence_contract": {"not_applicable": ["provider_task"]},
                "price_keys": ["image.banana.nb2.std", "image.banana.nb2.hd"],
                "smoke_inputs": ["短提示词"],
                "validation": _image_validation(),
            },
            {
                "key": "image.banana.nb2.reference",
                "name": "纳米香蕉 2 · 参考图生成",
                "frontend_selector": '[data-variant="nb2"]',
                "entrypoints": [_endpoint("POST", "/api/gen/banana")],
                "task_match": {"kind": "image", "source_page": "banana", "provider": "banana", "model": "nb2", "reference_count": ">0"},
                "evidence_contract": {"not_applicable": ["provider_task"]},
                "price_keys": ["image.banana.nb2.std", "image.banana.nb2.hd"],
                "smoke_inputs": ["短提示词", "1 张低成本参考图"],
                "validation": _image_validation(reference=True),
            },
            {
                "key": "image.banana.pro.text",
                "name": "纳米香蕉 Pro · 文生图",
                "frontend_selector": '[data-variant="pro"]',
                "entrypoints": [_endpoint("POST", "/api/gen/banana")],
                "task_match": {"kind": "image", "source_page": "banana", "provider": "banana", "model": "pro", "reference_count": 0},
                "evidence_contract": {"not_applicable": ["provider_task"]},
                "price_keys": ["image.banana.pro.std", "image.banana.pro.hd"],
                "smoke_inputs": ["短提示词"],
                "validation": _image_validation(),
            },
            {
                "key": "image.banana.pro.reference",
                "name": "纳米香蕉 Pro · 参考图生成",
                "frontend_selector": '[data-variant="pro"]',
                "entrypoints": [_endpoint("POST", "/api/gen/banana")],
                "task_match": {"kind": "image", "source_page": "banana", "provider": "banana", "model": "pro", "reference_count": ">0"},
                "evidence_contract": {"not_applicable": ["provider_task"]},
                "price_keys": ["image.banana.pro.std", "image.banana.pro.hd"],
                "smoke_inputs": ["短提示词", "1 张低成本参考图"],
                "validation": _image_validation(reference=True),
            },
        ],
    },
    {
        "key": "openai",
        "name": "黄雀引擎 2",
        "desc": "支持文生图、参考图生成与局部修改",
        "order": 20,
        "frontend_selector": '[data-engine="gpt"]',
        "service": "content",
        "dependencies": [
            {"key": "openai", "role": "主生成", "requirement": "required", "credential_source": "env"},
        ],
        "modes": [
            {
                "key": "image.openai.text",
                "name": "文生图",
                "entrypoints": [_endpoint("POST", "/api/gen/image")],
                "task_match": {"kind": "image", "source_page": "banana", "provider": "openai", "reference_count": 0, "mask_present": False},
                "evidence_contract": {"not_applicable": ["provider_task"]},
                "price_keys": ["image.openai.std", "image.openai.hd"],
                "smoke_inputs": ["短提示词"],
                "validation": _image_validation(),
            },
            {
                "key": "image.openai.reference",
                "name": "参考图生成",
                "entrypoints": [_endpoint("POST", "/api/gen/image")],
                "task_match": {"kind": "image", "source_page": "banana", "provider": "openai", "reference_count": ">0", "mask_present": False},
                "evidence_contract": {"not_applicable": ["provider_task"]},
                "price_keys": ["image.openai.std", "image.openai.hd"],
                "smoke_inputs": ["短提示词", "1 张参考图"],
                "validation": _image_validation(reference=True),
            },
            {
                "key": "image.openai.inpaint",
                "name": "局部修改",
                "frontend_selector": "#inpBtn",
                "entrypoints": [_endpoint("POST", "/api/gen/image")],
                "task_match": {"kind": "image", "source_page": "banana", "provider": "openai", "reference_count": ">0", "mask_present": True},
                "evidence_contract": {"not_applicable": ["provider_task"]},
                "price_keys": ["image.openai.std", "image.openai.hd"],
                "smoke_inputs": ["修改提示词", "1 张参考图", "1 张涂抹蒙版"],
                "validation": _image_validation(inpaint=True),
            },
        ],
    },
    {
        "key": "seedream",
        "name": "黄雀引擎 1",
        "desc": "选择标准或 Pro，支持文生图与多图参考生成",
        "order": 30,
        "frontend_selector": '[data-engine="seedream"]',
        "service": "content",
        "dependencies": [
            {"key": "seedance", "role": "火山方舟主生成", "requirement": "required", "credential_source": "env"},
        ],
        "modes": [
            {
                "key": "image.seedream.std.text", "name": "标准 · 文生图",
                "frontend_selector": '#seedreamVariantRow [data-variant="std"]',
                "entrypoints": [_endpoint("POST", "/api/gen/image")],
                "task_match": {"kind": "image", "source_page": "banana", "provider": "seedream", "variant": "std", "reference_count": 0},
                "evidence_contract": {"not_applicable": ["provider_task"]},
                "price_keys": ["image.seedream.std.std", "image.seedream.std.hd"],
                "smoke_inputs": ["短提示词"],
                "validation": _image_validation(),
            },
            {
                "key": "image.seedream.std.reference", "name": "标准 · 参考图生成",
                "frontend_selector": '#seedreamVariantRow [data-variant="std"]',
                "entrypoints": [_endpoint("POST", "/api/gen/image")],
                "task_match": {"kind": "image", "source_page": "banana", "provider": "seedream", "variant": "std", "reference_count": ">0"},
                "evidence_contract": {"not_applicable": ["provider_task"]},
                "price_keys": ["image.seedream.std.std", "image.seedream.std.hd"],
                "smoke_inputs": ["短提示词", "1 张参考图"],
                "validation": _image_validation(reference=True),
            },
            {
                "key": "image.seedream.pro.text", "name": "Pro · 文生图",
                "frontend_selector": '#seedreamVariantRow [data-variant="pro"]',
                "entrypoints": [_endpoint("POST", "/api/gen/image")],
                "task_match": {"kind": "image", "source_page": "banana", "provider": "seedream", "variant": "pro", "reference_count": 0},
                "evidence_contract": {"not_applicable": ["provider_task"]},
                "price_keys": ["image.seedream.pro.std", "image.seedream.pro.hd"],
                "smoke_inputs": ["短提示词"],
                "validation": _image_validation(),
            },
            {
                "key": "image.seedream.pro.reference", "name": "Pro · 参考图生成",
                "frontend_selector": '#seedreamVariantRow [data-variant="pro"]',
                "entrypoints": [_endpoint("POST", "/api/gen/image")],
                "task_match": {"kind": "image", "source_page": "banana", "provider": "seedream", "variant": "pro", "reference_count": ">0"},
                "evidence_contract": {"not_applicable": ["provider_task"]},
                "price_keys": ["image.seedream.pro.std", "image.seedream.pro.hd"],
                "smoke_inputs": ["短提示词", "1 张参考图"],
                "validation": _image_validation(reference=True),
            },
        ],
    },
    {
        "key": "xiaole",
        "name": "果肉生图",
        "desc": "支持文生图与多图参考生成",
        "order": 40,
        "frontend_selector": '[data-engine="xiaole"]',
        "service": "content",
        "dependencies": [
            {"key": "xiaolevideo", "role": "主生成", "requirement": "required", "credential_source": "env"},
        ],
        "modes": [
            {
                "key": "image.xiaole.text", "name": "文生图",
                "entrypoints": [_endpoint("POST", "/api/gen/image")],
                "task_match": {"kind": "image", "source_page": "banana", "provider": "xiaole", "reference_count": 0},
                "evidence_contract": {"not_applicable": []},
                "price_keys": ["image.xiaole.std", "image.xiaole.hd"],
                "smoke_inputs": ["短提示词"],
                "validation": _image_validation(),
            },
            {
                "key": "image.xiaole.reference", "name": "参考图生成",
                "entrypoints": [_endpoint("POST", "/api/gen/image")],
                "task_match": {"kind": "image", "source_page": "banana", "provider": "xiaole", "reference_count": ">0"},
                "evidence_contract": {"not_applicable": []},
                "price_keys": ["image.xiaole.std", "image.xiaole.hd"],
                "smoke_inputs": ["短提示词", "1 张参考图"],
                "validation": _image_validation(reference=True),
            },
        ],
    },
]


AUDIO_FUNCTIONS = [
    {
        "key": "tts",
        "name": "AI 配音",
        "desc": "输入文案，选择公共或个人音色，并调整语速、音调与音量生成配音",
        "order": 10,
        "frontend_selector": "#generateBtn",
        "service": "content",
        "flag_keys": ["audio"],
        "dependencies": [{
            "key": "cosyvoice", "role": "公共与个人音色合成",
            "requirement": "required", "credential_source": "env",
        }],
        "evidence_gaps": [
            "CosyVoice 同步合成不提供可稳定落库的上游 task_id/request_id",
            "当前鉴权探针可验证 DASHSCOPE_API_KEY，但没有可归一化的余额字段",
        ],
        "modes": [
            {
                "key": "audio.tts.public",
                "name": "公共音色配音",
                "frontend_selector": '[data-voice-tab="public"]',
                "entrypoints": [
                    _endpoint("POST", "/api/gen/audio"),
                    _endpoint("GET", "/api/gen/audio/voices"),
                    _endpoint("GET", "/api/gen/job/{id}"),
                    _endpoint("GET", "/api/gen/audio/assets"),
                ],
                "task_match": {
                    "kind": "audio", "source_page": "audio",
                    "provider": "cosyvoice", "voice_scope": "public",
                },
                "evidence_contract": {"not_applicable": ["provider_task", "balance"]},
                "price_keys": ["audio.tts"],
                "smoke_inputs": ["短配音文案", "任一公共音色", "语速/音调/音量默认值"],
                "validation": _validation({
                    "text": QA_AUDIO_TEXT,
                    "voice_scope": "public",
                    "speed": 1.0,
                    "pitch": 0,
                    "volume": 0,
                }),
            },
            {
                "key": "audio.tts.personal",
                "name": "个人音色配音",
                "frontend_selector": '[data-voice-tab="personal"]',
                "entrypoints": [
                    _endpoint("POST", "/api/gen/audio"),
                    _endpoint("GET", "/api/gen/audio/voices"),
                    _endpoint("GET", "/api/gen/job/{id}"),
                    _endpoint("GET", "/api/gen/audio/assets"),
                ],
                "task_match": {
                    "kind": "audio", "source_page": "audio",
                    "provider": "cosyvoice", "voice_scope": "personal",
                },
                "evidence_contract": {"not_applicable": ["provider_task", "balance"]},
                "price_keys": ["audio.tts"],
                "smoke_inputs": ["短配音文案", "一个已就绪的个人音色", "语速/音调/音量默认值"],
                "validation": _validation({
                    "text": QA_AUDIO_TEXT,
                    "voice_scope": "personal",
                    "speed": 1.0,
                    "pitch": 0,
                    "volume": 0,
                }),
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
        "inventory_status": "verified" if key in {"banana", "video", "audio"} else "pending",
        "functions": (
            VIDEO_FUNCTIONS if key == "video"
            else IMAGE_FUNCTIONS if key == "banana"
            else AUDIO_FUNCTIONS if key == "audio"
            else []
        ),
        "auxiliary_actions": ([{
            "key": "video.asset.import_h3", "name": "导入 H3 成片",
            "entrypoint": _endpoint("POST", "/api/gen/video/import"),
            "scope": "资产导入，不计入生成渠道健康率",
        }] if key == "video" else [{
            "key": "image.prompt.optimize", "name": "优化提示词",
            "entrypoint": _endpoint("POST", "/api/gen/reverse"),
            "scope": "同步免费工具，不创建生成任务",
        }, {
            "key": "image.prompt.reverse", "name": "反推提示词",
            "entrypoint": _endpoint("POST", "/api/gen/reverse"),
            "scope": "同步扣点工具，不创建生成任务",
            "price_keys": ["image.reverse"],
        }] if key == "banana" else []),
    }
    for order, (key, name, path) in enumerate(_PAGE_DEFS)
]


def list_pages():
    pages = deepcopy(FUNCTION_REGISTRY)
    for page in pages:
        for feature in page["functions"]:
            for mode in feature.get("modes", []):
                private = mode.get("validation") or {}
                prefill = private.get("prefill") or {}
                prompt = str(prefill.get("prompt") or prefill.get("text") or "")
                mode["validation"] = {
                    "supported": bool(private.get("supported")),
                    "blocked_reason": private.get("blocked_reason") or "",
                    "manual_requirements": private.get("manual_requirements") or [],
                    "fixture_summary": list(mode.get("smoke_inputs") or []),
                    "prompt_preview": prompt,
                    "requires_paid_confirmation": True,
                }
    return pages


def e2e_runner(operation_key):
    """Return one private runner contract; never send this object to a browser."""
    for page in FUNCTION_REGISTRY:
        for feature in page["functions"]:
            for mode in feature.get("modes", []):
                if mode["key"] == operation_key:
                    private = deepcopy(mode.get("validation") or {})
                    private.update({
                        "operation_id": mode["key"],
                        "endpoint": deepcopy(mode["entrypoints"][0]),
                        "evidence_contract": deepcopy(mode.get("evidence_contract") or {}),
                    })
                    return private
    return None


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
    source_page = str(metadata.get("source_page") or "").strip().lower()
    if source_page not in {"", "video", "banana", "audio"}:
        return None
    if kind == "image" and source_page != "banana":
        return None
    if kind != "image" and source_page == "banana":
        return None
    if kind == "audio" and source_page != "audio":
        return None
    if kind != "audio" and source_page == "audio":
        return None
    try:
        references = int(metadata.get("reference_count") or (1 if metadata.get("image") else 0))
    except (TypeError, ValueError):
        references = 0
    channel = str(metadata.get("channel") or "").strip().lower()
    actual = {
        "kind": kind,
        "mode": str(metadata.get("mode") or ("text" if kind == "video" else "")).strip().lower(),
        "cine_mode": str(metadata.get("cine_mode") or "").strip().lower(),
        "line": str(metadata.get("line") or "").strip(),
        "channel": channel,
        "source_page": source_page,
        "provider": str(metadata.get("provider") or ("openai" if kind == "image" else "")).strip().lower(),
        "model": str(metadata.get("model") or "").strip().lower(),
        "variant": str(metadata.get("variant") or "").strip().lower(),
        "voice_scope": str(metadata.get("voice_scope") or "").strip().lower(),
        "mask_present": bool(metadata.get("mask_present") or metadata.get("mask")),
        "operation": str(
            metadata.get("operation")
            or ("generate" if kind == "xiaole_video" and channel == "grok" else "")
        ).strip().lower(),
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
            if not feature.get("frontend_selector") or not feature.get("service"):
                raise ValueError("customer feature needs a frontend entry and business service")
            shared_steps = feature.get("shared_steps", [])
            modes = feature.get("modes", [])
            if not modes:
                raise ValueError("customer feature needs at least one mode")
            leaves = shared_steps + modes
            operation_keys.extend(leaf["key"] for leaf in leaves)
            for leaf in leaves:
                if not leaf.get("entrypoints"):
                    raise ValueError("registry operation needs a business entrypoint")
                if not (leaf.get("task_match") or leaf.get("evidence_source")):
                    raise ValueError("registry operation needs a durable evidence source")
                for dependency in feature.get("dependencies", []) + leaf.get("dependencies", []):
                    requirement = dependency.get("requirement")
                    if requirement not in {"required", "optional", "alternative"}:
                        raise ValueError("invalid dependency requirement")
                    if requirement == "alternative" and not dependency.get("alternative_group"):
                        raise ValueError("alternative dependency needs a group")
                    if dependency.get("credential_source") not in {None, "env", "pool"}:
                        raise ValueError("invalid credential source")
            for mode in modes:
                if not mode.get("smoke_inputs"):
                    raise ValueError("customer mode needs reusable test inputs")
                if "price_keys" not in mode:
                    raise ValueError("customer mode needs an explicit billing contract")
                validation = mode.get("validation") or {}
                if "supported" not in validation:
                    raise ValueError("customer mode needs an explicit E2E runner state")
                if validation.get("supported") and not validation.get("prefill"):
                    raise ValueError("runnable customer mode needs private fixtures")
                if not validation.get("supported") and not validation.get("blocked_reason"):
                    raise ValueError("blocked E2E runner needs a reason")
                invalid = set((mode.get("evidence_contract") or {}).get("not_applicable", [])) - {
                    "provider_task", "balance", "billing",
                }
                if invalid:
                    raise ValueError("unknown not-applicable evidence step")
    if len(operation_keys) != len(set(operation_keys)):
        raise ValueError("duplicate operation key")
    return True


validate_registry()
