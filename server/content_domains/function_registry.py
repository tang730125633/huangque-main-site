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
QA_TEXT_VIDEO_TOPIC = "AI 如何帮助一家本地门店把复杂服务讲得更清楚"
QA_TEXT_VIDEO_SCRIPT = "先说清客户的问题。\n\n再用一个具体场景展示解决方式。"
QA_COLLECT_URL = "@env/HQ_E2E_COLLECT_URL"
QA_LEADS_KEYWORD = "美容院如何拓客"
QA_SCRIPT_PROMPT = "为一款无品牌的琥珀色保湿精华写一条可拍摄短视频脚本，卖点是清爽、易吸收；不得虚构功效、价格或品牌信息"
QA_CANVAS_PROMPT = "根据画布中的产品卖点，创建一条短视频文案草稿和一条图片生成草稿；只规划，不执行媒体生成"
QA_SHORT_DRAMA_SOURCE = """场景一 旧书店 日 内
林夏整理旧相册，发现一张被撕掉一半的合影。
林夏：这张照片为什么只剩下一半？
周野推门进来，把另一半照片放在桌上。
周野：因为另一半，一直在等你回来。
场景二 旧书店门口 黄昏 外
雨停了，两人把照片拼好，安静地看向镜头。
林夏：那就从今天开始，把故事补完整。"""
QA_SHORT_DRAMA_PREVIEW_SOURCE = """场景一 旧书店 日 内
林夏走进旧书店，拉开窗帘，晨光落在一本旧相册上。
林夏拿起相册，拂去灰尘，在书架间缓慢翻看。
林夏：有些故事，只是暂时被遗忘了。
她发现一张折叠的便签，将它夹回相册。
场景二 旧书店门口 黄昏 外
林夏抱着相册走到门口，雨后的街道映着暖色灯光。
林夏合上相册，微笑着看向镜头。
林夏：今天，我们把它重新写下来。"""
QA_SHORT_DRAMA_CHARACTER_CONTRACT = [{
    "character_key": "character_1", "name": "林夏", "role_type": "main",
    "gender": "女", "age": "28 岁", "identity_text": "旧书店店主",
    "relationships": "周野的旧友", "personality": "安静、坚定、克制",
    "face_shape": "自然鹅蛋脸", "hairstyle": "齐肩微卷发",
    "hair_color": "深棕色", "height_body": "中等身高、匀称体型",
    "fixed_clothing": "米白色针织衫与深棕色长裙",
    "fixed_colors": "米白、深棕", "accessories": "无",
    "appearance_prompt": "28 岁东亚女性，自然鹅蛋脸，齐肩深棕微卷发，中等身高，匀称体型",
    "wardrobe_prompt": "米白色针织衫，深棕色长裙，无配饰",
    "reference_views": ["front_full", "side_full", "front_half"],
}]


def _validation(prefill=None, manual_requirements=None, supported=True, blocked_reason="",
                browser=None):
    """Private server-side fixture contract; list_pages() strips its inputs."""
    return {
        "prefill": deepcopy(prefill or {}),
        "manual_requirements": list(manual_requirements or []),
        "supported": bool(supported),
        "blocked_reason": str(blocked_reason or ""),
        "browser": deepcopy(browser or {}),
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
        "desc": "使用文本或人物参考图生成 2K 剧情短片",
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
            "name": "文本 / 多参考图生视频",
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


def _image_validation(reference=False, inpaint=False, browser=False):
    prefill = {
        "prompt": QA_IMAGE_EDIT_PROMPT if inpaint else QA_IMAGE_PROMPT,
        "ratio": "1:1", "quality": "std", "count": 1,
    }
    if inpaint:
        prefill.update({"image_url": QA_PRODUCT_IMAGE, "mask_url": QA_PRODUCT_IMAGE})
    elif reference:
        prefill["reference_images"] = [QA_PRODUCT_IMAGE]
    return _validation(prefill, browser={
        "supported": True,
        "origin": "https://huangquechuanmei.com",
        "fixture": "qa-serum.png",
        "checks": [
            "客户页面打开", "预设素材加载", "客户按钮真实提交",
            "作品在客户页可见", "作品下载与解码", "同一 job_id 关联生产证据",
        ],
        "timeout_seconds": 300,
    } if browser else None)


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
                "validation": _image_validation(reference=True, browser=True),
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
        "flag_keys": ["image_xiaole"],
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


COLLECT_FUNCTIONS = [
    {
        "key": "collect",
        "name": "内容采集",
        "desc": "从抖音或小红书链接采集内容、解析视频或提取口播文案",
        "order": 10,
        "frontend_selector": "#colBtn",
        "service": "leadgen",
        "flag_keys": ["collect"],
        "dependencies": [
            {"key": "tikhub", "role": "内容解析与评论采集", "requirement": "required",
             "credential_source": "env"},
            {"key": "cos", "role": "采集素材长期存储", "requirement": "optional",
             "condition": "未配置或转存失败时回退原始素材地址"},
        ],
        "modes": [
            {
                "key": "collect.content.comments",
                "name": "内容与评论采集",
                "frontend_selector": "#tabLink",
                "entrypoints": [_endpoint("POST", "/api/gen/collect")],
                "task_match": {"kind": "collect", "collect_mode": "comments"},
                "evidence_contract": {"not_applicable": ["provider_task", "balance"]},
                "price_keys": ["collect.base"],
                "smoke_inputs": ["1 条已授权固定内容链接", "标题/正文/媒体与评论结果"],
                "validation": _validation({
                    "url": QA_COLLECT_URL, "want": ["comments"],
                    "provider": "tikhub", "source_page": "collect",
                }),
            },
            {
                "key": "collect.content.video",
                "name": "视频解析下载",
                "frontend_selector": "#tabDl",
                "entrypoints": [
                    _endpoint("POST", "/api/gen/collect"),
                    _endpoint("GET", "/api/gen/dl"),
                ],
                "task_match": {"kind": "collect", "collect_mode": "video"},
                "evidence_contract": {"not_applicable": ["provider_task", "balance"]},
                "price_keys": ["collect.base"],
                "smoke_inputs": ["1 条已授权固定视频链接", "可下载的视频结果"],
                "validation": _validation({
                    "url": QA_COLLECT_URL, "want": ["video"],
                    "provider": "tikhub", "source_page": "collect",
                }),
            },
            {
                "key": "collect.content.transcript",
                "name": "口播文案提取",
                "frontend_selector": "#transcriptExtractBtn",
                "entrypoints": [_endpoint("POST", "/api/gen/collect")],
                "task_match": {"kind": "collect", "collect_mode": "transcript"},
                "evidence_contract": {"not_applicable": ["provider_task", "balance"]},
                "dependencies": [
                    {"key": "openai", "role": "无字幕视频的口播识别", "requirement": "optional",
                     "credential_source": "env", "condition": "素材没有平台字幕时调用"},
                ],
                "price_keys": ["collect.base", "collect.transcript_extra"],
                "smoke_inputs": ["1 条已授权固定短视频链接", "非空口播文案"],
                "validation": _validation({
                    "url": QA_COLLECT_URL, "want": ["transcript"],
                    "provider": "tikhub", "source_page": "collect",
                }),
            },
        ],
    },
]


LEADS_FUNCTIONS = [
    {
        "key": "keyword",
        "name": "关键词获客",
        "desc": "按关键词搜索公开内容评论并筛选可跟进线索",
        "order": 10,
        "frontend_selector": "#goBtn",
        "service": "leadgen",
        "flag_keys": ["leads"],
        "dependencies": [{
            "key": "tikhub", "role": "公开内容搜索与评论采集",
            "requirement": "required", "credential_source": "env",
        }],
        "modes": [{
            "key": "leads.keyword.search",
            "name": "关键词筛选线索",
            "entrypoints": [_endpoint("POST", "/api/gen/leads")],
            "task_match": {"kind": "leads"},
            "evidence_contract": {"not_applicable": ["provider_task", "balance"]},
            "price_keys": ["leads.base", "leads.per_four"],
            "smoke_inputs": ["预设通用关键词", "抖音公开内容", "最低采集量 1"],
            "validation": _validation({
                "keyword": QA_LEADS_KEYWORD, "platforms": ["douyin"],
                "count": 1, "pages": 1, "channels_targets": [],
                "provider": "tikhub", "source_page": "leads",
            }),
        }],
    },
]


SCRIPT_FUNCTIONS = [
    {
        "key": "script_writer",
        "name": "AI 写脚本",
        "desc": "按客户选择的口播、剧情或种草风格生成可编辑分镜脚本",
        "order": 10,
        "frontend_selector": '#scModeTabs [data-mode="write"]',
        "service": "content",
        "dependencies": [{
            "key": "openai", "role": "文案模型兼容线路", "requirement": "optional",
            "credential_source": "env", "condition": "未配置专用文案线路时回退",
        }],
        "evidence_gaps": ["专用 COPY_API / 智谱线路尚未归一到渠道凭据面板"],
        "modes": [
            {
                "key": "script.write.spoken", "name": "口播脚本",
                "frontend_selector": '#segStyle .sc-opt:nth-child(1)',
                "entrypoints": [_endpoint("POST", "/api/gen/copy"), _endpoint("GET", "/api/gen/job/{id}")],
                "task_match": {"kind": "copy", "source_page": "script", "format": "script", "style": "口播"},
                "evidence_contract": {"not_applicable": ["provider_task", "balance"]},
                "price_keys": ["text.copy"],
                "smoke_inputs": ["预设合规产品选题与卖点", "口播", "15 秒", "抖音"],
                "validation": _validation({
                    "prompt": QA_SCRIPT_PROMPT, "format": "script", "style": "口播",
                    "dur": "15s", "platform": "抖音", "ctype": "分镜脚本",
                    "source_page": "script",
                }),
            },
            {
                "key": "script.write.story", "name": "剧情脚本",
                "frontend_selector": '#segStyle .sc-opt:nth-child(2)',
                "entrypoints": [_endpoint("POST", "/api/gen/copy"), _endpoint("GET", "/api/gen/job/{id}")],
                "task_match": {"kind": "copy", "source_page": "script", "format": "script", "style": "剧情"},
                "evidence_contract": {"not_applicable": ["provider_task", "balance"]},
                "price_keys": ["text.copy"],
                "smoke_inputs": ["预设合规产品选题与卖点", "剧情", "15 秒", "抖音"],
                "validation": _validation({
                    "prompt": QA_SCRIPT_PROMPT, "format": "script", "style": "剧情",
                    "dur": "15s", "platform": "抖音", "ctype": "分镜脚本",
                    "source_page": "script",
                }),
            },
            {
                "key": "script.write.recommend", "name": "种草脚本",
                "frontend_selector": '#segStyle .sc-opt:nth-child(3)',
                "entrypoints": [_endpoint("POST", "/api/gen/copy"), _endpoint("GET", "/api/gen/job/{id}")],
                "task_match": {"kind": "copy", "source_page": "script", "format": "script", "style": "种草"},
                "evidence_contract": {"not_applicable": ["provider_task", "balance"]},
                "price_keys": ["text.copy"],
                "smoke_inputs": ["预设合规产品选题与卖点", "种草", "15 秒", "抖音"],
                "validation": _validation({
                    "prompt": QA_SCRIPT_PROMPT, "format": "script", "style": "种草",
                    "dur": "15s", "platform": "抖音", "ctype": "分镜脚本",
                    "source_page": "script",
                }),
            },
        ],
    },
    {
        "key": "breakdown",
        "name": "拆解视频",
        "desc": "从公开视频链接生成分镜拆解或视频提示词反推",
        "order": 20,
        "frontend_selector": '#scModeTabs [data-mode="breakdown"]',
        "service": "content",
        "flag_keys": ["breakdown"],
        "dependencies": [
            {"key": "tikhub", "role": "公开视频下载与素材信息", "requirement": "required", "credential_source": "env"},
            {"key": "openai", "role": "无字幕视频的语音识别", "requirement": "optional", "credential_source": "env", "condition": "视频无可用字幕时调用"},
            {"key": "zhipu", "role": "链接分镜拆解", "requirement": "required", "credential_source": "env", "condition": "mode=scenes"},
            {"key": "gemini", "role": "链接视频提示词反推", "requirement": "required", "credential_source": "env", "condition": "mode=reverse_prompt"},
        ],
        "modes": [
            {
                "key": "script.breakdown.scenes", "name": "链接分解拆解",
                "frontend_selector": "#bdToolScenes",
                "entrypoints": [_endpoint("POST", "/api/gen/breakdown"), _endpoint("GET", "/api/gen/job/{id}")],
                "task_match": {"kind": "breakdown", "source_page": "script", "mode": "scenes"},
                "evidence_contract": {"not_applicable": ["provider_task", "balance"]},
                "price_keys": ["breakdown.item"],
                "smoke_inputs": ["1 条已授权固定视频链接", "分镜结构结果"],
                "validation": _validation({"url": QA_COLLECT_URL, "mode": "scenes", "source_page": "script"}),
            },
            {
                "key": "script.breakdown.reverse", "name": "链接提示词反推",
                "frontend_selector": "#bdToolReverse",
                "entrypoints": [_endpoint("POST", "/api/gen/breakdown"), _endpoint("GET", "/api/gen/job/{id}")],
                "task_match": {"kind": "breakdown", "source_page": "script", "mode": "reverse_prompt", "source_type": ""},
                "evidence_contract": {"not_applicable": ["provider_task", "balance"]},
                "price_keys": ["breakdown.item"],
                "smoke_inputs": ["1 条已授权固定视频链接", "非空视频提示词"],
                "validation": _validation({"url": QA_COLLECT_URL, "mode": "reverse_prompt", "source_page": "script"}),
            },
            {
                "key": "script.breakdown.local_image", "name": "本地图片反推",
                "frontend_selector": "#bdImageReverse",
                "entrypoints": [_endpoint("POST", "/api/gen/breakdown/local-upload?media_type=image")],
                "task_match": {"kind": "breakdown", "source_page": "script", "source_type": "image"},
                "evidence_contract": {"not_applicable": ["provider_task", "balance"]},
                "price_keys": ["breakdown.item"],
                "smoke_inputs": ["1 张本地图片", "非空图片提示词"],
                "validation": _validation({"file_url": QA_PRODUCT_IMAGE, "media_type": "image"}),
            },
            {
                "key": "script.breakdown.local_video", "name": "本地视频反推",
                "frontend_selector": "#bdVideoReverse",
                "entrypoints": [_endpoint("POST", "/api/gen/breakdown/local-upload?media_type=video")],
                "task_match": {"kind": "breakdown", "source_page": "script", "source_type": "video"},
                "evidence_contract": {"not_applicable": ["provider_task", "balance"]},
                "price_keys": ["breakdown.item"],
                "smoke_inputs": ["1 段本地短视频", "非空视频提示词"],
                "validation": _validation({"file_url": QA_MOTION_VIDEO, "media_type": "video"}),
            },
        ],
    },
    {
        "key": "output",
        "name": "脚本结果生产",
        "desc": "把当前脚本或拆解结果继续生成剧情视频、数字人口播、同款视频或图片",
        "order": 30,
        "frontend_selector": "#scGenVideo, #scGenAudio, #bdRemakeBtn, #bdToImageBtn",
        "service": "content",
        "modes": [
            {
                "key": "script.output.video.story", "name": "一键生成剧情视频",
                "entrypoints": [_endpoint("POST", "/api/gen/script_to_video")],
                "task_match": {"kind": "script_to_video", "source_page": "script", "pipeline": "", "style": "剧情"},
                "price_keys": ["video.grok.v1.720p"],
                "smoke_inputs": ["已生成剧情分镜", "分镜画面与时长"],
                "validation": _validation(supported=False, blocked_reason="尚未准备可重复使用的剧情分镜测试包"),
            },
            {
                "key": "script.output.video.spoken", "name": "一键生成口播视频",
                "entrypoints": [_endpoint("POST", "/api/gen/script_to_video")],
                "task_match": {"kind": "script_to_video", "source_page": "script", "pipeline": "", "style": "口播"},
                "price_keys": ["video.talking.block"],
                "smoke_inputs": ["已生成口播分镜", "已就绪形象", "公共音色"],
                "validation": _validation(supported=False, blocked_reason="尚未登记编导页专用形象与口播测试包"),
            },
            {
                "key": "script.output.video.recommend", "name": "一键生成种草口播",
                "entrypoints": [_endpoint("POST", "/api/gen/script_to_video")],
                "task_match": {"kind": "script_to_video", "source_page": "script", "pipeline": "", "style": "种草"},
                "price_keys": ["video.talking.block"],
                "smoke_inputs": ["已生成种草分镜", "已就绪形象", "公共音色"],
                "validation": _validation(supported=False, blocked_reason="尚未登记编导页专用形象与种草口播测试包"),
            },
            {
                "key": "script.output.remake.cinematic", "name": "同款视频 · 电影化身",
                "entrypoints": [_endpoint("POST", "/api/gen/cinematic")],
                "task_match": {"kind": "cinematic", "source_page": "script", "cine_mode": "open"},
                "price_keys": ["video.cinematic.open"],
                "smoke_inputs": ["反推提示词", "参考关键帧", "已就绪形象"],
                "validation": _validation(supported=False, blocked_reason="尚未登记编导同款专用形象测试包"),
            },
            {
                "key": "script.output.remake.grok", "name": "同款视频 · 果肉",
                "entrypoints": [_endpoint("POST", "/api/gen/xiaole_video")],
                "task_match": {"kind": "xiaole_video", "source_page": "script", "channel": "grok"},
                "price_keys": ["video.grok.v1.720p", "video.grok.v1_5.720p"],
                "smoke_inputs": ["反推提示词", "参考关键帧"],
                "validation": _validation(supported=False, blocked_reason="尚未准备编导同款关键帧测试包"),
            },
            {
                "key": "script.output.remake.micro", "name": "同款视频 · Seedance",
                "entrypoints": [_endpoint("POST", "/api/gen/xiaole_video")],
                "task_match": {"kind": "xiaole_video", "source_page": "script", "channel": "micro"},
                "price_keys": ["video.seedance"],
                "smoke_inputs": ["反推提示词", "参考关键帧"],
                "validation": _validation(supported=False, blocked_reason="尚未准备编导同款关键帧测试包"),
            },
            {
                "key": "script.output.image", "name": "拆解结果一键做图",
                "entrypoints": [_endpoint("POST", "/api/gen/image")],
                "task_match": {"kind": "image", "source_page": "script"},
                "evidence_contract": {"not_applicable": ["provider_task"]},
                "price_keys": ["image.openai.std"],
                "smoke_inputs": ["已生成分镜", "分镜画面描述"],
                "validation": _image_validation(),
            },
        ],
    },
]


SHORT_DRAMA_FUNCTIONS = [{
    "key": "live_action",
    "name": "AI 真人短剧",
    "desc": "从真人剧本、角色确认、逐镜生产到预览与交付的项目型生产线",
    "order": 10,
    "frontend_selector": '[data-content-type="live_action"]',
    "service": "content",
    "modes": [
        {
            "key": "short_drama.live_action.script_planning", "name": "剧本与分镜策划",
            "entrypoints": [
                _endpoint("POST", "/api/gen/short-drama/projects/import"),
                _endpoint("POST", "/api/gen/short-drama/conversation/messages"),
                _endpoint("POST", "/api/gen/short-drama/conversation/script/generate"),
                _endpoint("POST", "/api/gen/short-drama/conversation/script/lock"),
                _endpoint("POST", "/api/gen/short-drama/preflight/generate"),
                _endpoint("POST", "/api/gen/short-drama/preflight/confirm"),
                _endpoint("POST", "/api/gen/short-drama/projects/live-action/abandon"),
            ],
            "evidence_source": "short_drama_projects", "evidence_contract": {"acceptance_id_type": "project_id", "not_applicable": ["provider_task", "billing", "balance"]},
            "price_keys": [], "smoke_inputs": ["固定真人短剧脚本", "两个固定角色", "首版剧本与制作预检"],
            "validation": _validation(prefill={
                "title": "后台质检 · 被补完整的合影",
                "synopsis": "两位旧友在旧书店重逢，并把一张被撕开的合影重新拼好。",
                "ratio": "16:9", "target_duration": 30, "shot_count": 6,
                "visual_style": "电影感写实", "source_text": QA_SHORT_DRAMA_SOURCE,
                "filename": "后台质检真人短剧.txt", "import_mode": "faithful",
                "content_type": "live_action", "character_contract": [],
            }),
        },
        {
            "key": "short_drama.live_action.character_reference", "name": "角色标准图",
            "entrypoints": [_endpoint("POST", "/api/gen/short-drama/generate-character-reference"), _endpoint("POST", "/api/gen/short-drama/confirm-character-reference")],
            "dependencies": [{"key": "gemini", "role": "主生成", "requirement": "required", "credential_source": "env"}],
            "evidence_source": "short_drama_character_reference_jobs",
            "evidence_contract": {"not_applicable": ["provider_task"]},
            "price_keys": ["image.banana.nb2.hd"], "smoke_inputs": ["已确认角色卡", "角色标准图生成与锁定"],
            "validation": _validation(prefill={
                "title": "后台质检 · 角色标准图", "synopsis": "验证短剧角色标准图生成、锁定、交付和点数闭环。",
                "ratio": "16:9", "target_duration": 30, "shot_count": 6,
                "visual_style": "电影感写实", "source_text": QA_SHORT_DRAMA_SOURCE,
                "filename": "后台质检角色标准图.txt", "import_mode": "faithful",
                "content_type": "live_action",
                "character_contract": QA_SHORT_DRAMA_CHARACTER_CONTRACT,
            }),
        },
        {
            "key": "short_drama.live_action.shot_video", "name": "逐镜视频生成",
            "entrypoints": [_endpoint("POST", "/api/gen/short-drama/autodraft/provider-preflight"), _endpoint("POST", "/api/gen/short-drama/autodraft/provider-quote"), _endpoint("POST", "/api/gen/short-drama/autodraft/provider-jobs")],
            "dependencies": [{"key": "xai", "role": "单镜头主生成", "requirement": "required", "credential_source": "pool"}],
            "evidence_source": "short_drama_provider_shot_jobs", "price_keys": ["video.grok.v1.720p", "video.grok.v1_5.720p"],
            "smoke_inputs": ["已锁定角色与分镜", "单个最短镜头", "真实 Provider 任务与作品"],
            "validation": _validation(prefill={
                "title": "后台质检 · 单镜头视频", "synopsis": "验证短剧单镜头预检、报价、生成、交付和点数闭环。",
                "ratio": "16:9", "target_duration": 30, "shot_count": 6,
                "visual_style": "电影感写实", "source_text": QA_SHORT_DRAMA_SOURCE,
                "filename": "后台质检单镜头视频.txt", "import_mode": "faithful",
                "content_type": "live_action",
                "character_contract": QA_SHORT_DRAMA_CHARACTER_CONTRACT,
            }),
        },
        {
            "key": "short_drama.live_action.preview", "name": "短剧预览合成",
            "entrypoints": [_endpoint("POST", "/api/gen/short-drama/autodraft/jobs")],
            "dependencies": [{"key": "xai", "role": "逐镜素材生成", "requirement": "required", "credential_source": "pool"}],
            "evidence_source": "short_drama_autodraft_jobs",
            "price_keys": ["video.grok.v1.720p", "video.grok.v1_5.720p"],
            "smoke_inputs": ["已锁定逐镜素材", "720p 预览文件"],
            "validation": _validation(prefill={
                "title": "后台质检 · 短剧预览合成",
                "synopsis": "单角色在旧书店重新发现并记录一段被遗忘的故事。",
                "ratio": "16:9", "target_duration": 30, "shot_count": 6,
                "visual_style": "电影感写实", "source_text": QA_SHORT_DRAMA_PREVIEW_SOURCE,
                "filename": "后台质检短剧预览.txt", "import_mode": "faithful",
                "content_type": "live_action",
                "character_contract": QA_SHORT_DRAMA_CHARACTER_CONTRACT,
            }),
        },
        {
            "key": "short_drama.live_action.delivery", "name": "正式交付",
            "entrypoints": [_endpoint("POST", "/api/gen/short-drama/delivery/quote"), _endpoint("POST", "/api/gen/short-drama/delivery/jobs")],
            "evidence_source": "short_drama_delivery_jobs",
            "evidence_contract": {"acceptance_id_type": "project_id", "not_applicable": ["provider_task", "balance"]},
            "price_keys": [],
            "smoke_inputs": ["已验收的 30 秒私有预览", "1080p 正式交付文件", "0 点账务核对"],
            "validation": _validation(prefill={
                "title": "后台质检 · 短剧预览合成",
                "synopsis": "单角色在旧书店重新发现并记录一段被遗忘的故事。",
                "ratio": "16:9", "target_duration": 30, "shot_count": 6,
                "visual_style": "电影感写实", "source_text": QA_SHORT_DRAMA_PREVIEW_SOURCE,
                "filename": "后台质检短剧正式交付.txt", "import_mode": "faithful",
                "content_type": "live_action",
                "character_contract": QA_SHORT_DRAMA_CHARACTER_CONTRACT,
            }),
        },
    ],
}]


CANVAS_FUNCTIONS = [{
    "key": "agent",
    "name": "画布 Agent",
    "desc": "读取当前画布并返回需要测试人员确认后才会应用的创作计划",
    "order": 10,
    "frontend_selector": '[data-side="agent"]',
    "service": "content",
    "flag_keys": ["canvas_agent"],
    "dependencies": [{"key": "openai", "role": "结构化创作计划", "requirement": "required", "credential_source": "env"}],
    "modes": [{
        "key": "canvas.agent.plan", "name": "生成创作计划",
        "entrypoints": [_endpoint("POST", "/api/gen/canvas_agent"), _endpoint("POST", "/api/gen/canvas-agent/quote"), _endpoint("GET", "/api/gen/job/{id}")],
        "task_match": {"kind": "canvas_agent", "source_page": "canvas"},
        "evidence_contract": {"not_applicable": ["provider_task", "balance"]},
        "price_keys": ["canvas.agent"],
        "smoke_inputs": ["固定产品卖点文本节点", "预设创作要求", "只返回计划、不自动应用"],
        "validation": _validation({"prompt": QA_CANVAS_PROMPT, "source_page": "canvas"}),
    }],
}, {
    "key": "image_node", "name": "图片节点", "desc": "在画布节点内选择图片引擎生成作品",
    "order": 20, "frontend_selector": '[data-add="gen"]', "service": "content",
    "modes": [
        {
            "key": "canvas.image.banana.nb2", "name": "纳米香蕉 2",
            "entrypoints": [_endpoint("POST", "/api/gen/banana"), _endpoint("GET", "/api/gen/job/{id}")],
            "flag_keys": ["banana"],
            "dependencies": [{"key": "gemini", "role": "主生成", "requirement": "required", "credential_source": "env"}],
            "task_match": {"kind": "image", "source_page": "canvas", "provider": "banana", "model": "nb2"},
            "evidence_contract": {"not_applicable": ["provider_task"]},
            "price_keys": ["image.banana.nb2.hd"],
            "smoke_inputs": ["固定产品提示词", "画布图片节点结果写回"],
            "validation": _image_validation(),
        }, {
            "key": "canvas.image.banana.pro", "name": "纳米香蕉 Pro",
            "entrypoints": [_endpoint("POST", "/api/gen/banana"), _endpoint("GET", "/api/gen/job/{id}")],
            "flag_keys": ["banana"],
            "dependencies": [{"key": "gemini", "role": "主生成", "requirement": "required", "credential_source": "env"}],
            "task_match": {"kind": "image", "source_page": "canvas", "provider": "banana", "model": "pro"},
            "evidence_contract": {"not_applicable": ["provider_task"]},
            "price_keys": ["image.banana.pro.hd"],
            "smoke_inputs": ["固定产品提示词", "画布图片节点结果写回"],
            "validation": _image_validation(),
        }, {
            "key": "canvas.image.openai", "name": "黄雀引擎 2",
            "entrypoints": [_endpoint("POST", "/api/gen/image"), _endpoint("GET", "/api/gen/job/{id}")],
            "flag_keys": ["image"],
            "dependencies": [{"key": "openai", "role": "主生成", "requirement": "required", "credential_source": "env"}],
            "task_match": {"kind": "image", "source_page": "canvas", "provider": "openai"},
            "evidence_contract": {"not_applicable": ["provider_task"]},
            "price_keys": ["image.openai.hd"],
            "smoke_inputs": ["固定产品提示词", "画布图片节点结果写回"],
            "validation": _image_validation(),
        }, {
            "key": "canvas.image.zelong", "name": "泽龙 AI",
            "entrypoints": [_endpoint("POST", "/api/gen/image"), _endpoint("GET", "/api/gen/job/{id}")],
            "flag_keys": ["image"],
            "dependencies": [{"key": "zelong", "role": "主生成", "requirement": "required", "credential_source": "env"}],
            "task_match": {"kind": "image", "source_page": "canvas", "provider": "zelong"},
            "evidence_contract": {"not_applicable": ["provider_task"]},
            "price_keys": ["image.zelong.hd"],
            "smoke_inputs": ["固定产品提示词", "画布图片节点结果写回"],
            "validation": _image_validation(),
        },
    ],
}, {
    "key": "video_node", "name": "视频节点", "desc": "在画布节点内选择视频引擎生成作品",
    "order": 30, "frontend_selector": '[data-add="video"]', "service": "content",
    "alternative_selections": {"grok_provider": {"env": "GROK_VIDEO_PROVIDER", "default": "xai"}},
    "modes": [
        {
            "key": "canvas.video.grok", "name": "果肉视频",
            "entrypoints": [_endpoint("POST", "/api/gen/xiaole_video"), _endpoint("GET", "/api/gen/job/{id}")],
            "flag_keys": ["grok_video"],
            "dependencies": [{"key": "xai", "role": "主生成", "requirement": "alternative", "alternative_group": "grok_provider", "selection_value": "xai", "credential_source": "pool"}, {"key": "xiaolevideo", "role": "主生成", "requirement": "alternative", "alternative_group": "grok_provider", "selection_value": "xiaole"}],
            "task_match": {"kind": "xiaole_video", "source_page": "canvas", "channel": "grok"},
            "price_keys": ["video.grok.v1.720p"],
            "smoke_inputs": ["固定产品提示词", "画布视频节点业务结果"],
            "validation": _validation({
                "mode": "grok", "prompt": QA_PROMPT, "duration": 5,
                "resolution": "480p", "ratio": "16:9",
            }),
        }, {
            "key": "canvas.video.micro", "name": "豆姐视频",
            "entrypoints": [_endpoint("POST", "/api/gen/xiaole_video"), _endpoint("GET", "/api/gen/job/{id}")],
            "flag_keys": ["seedance_video"],
            "dependencies": [{"key": "seedance", "role": "主生成", "requirement": "required", "credential_source": "pool"}],
            "task_match": {"kind": "xiaole_video", "source_page": "canvas", "channel": "micro"},
            "price_keys": ["video.seedance"],
            "smoke_inputs": ["固定产品提示词", "画布视频节点业务结果"],
            "validation": _validation({
                "mode": "micro", "prompt": QA_PROMPT, "duration": 4,
                "resolution": "480p", "ratio": "16:9", "generate_audio": False,
            }),
        },
    ],
}]


TEXT_VIDEO_FUNCTIONS = [{
    "key": "text_video",
    "name": "文案成片",
    "desc": "从主题或完整文案生成旁白、画面素材并套用模板输出视频",
    "order": 10,
    "frontend_selector": '#generateBtn',
    "service": "content",
    "flag_keys": ["pixelle_text_video"],
    "modes": [{
        "key": "text_video.topic", "name": "主题创作",
        "frontend_selector": '[data-mode="generate"]',
        "entrypoints": [
            _endpoint("POST", "/api/gen/script_to_video"),
            _endpoint("GET", "/api/gen/text-video/templates"),
            _endpoint("GET", "/api/gen/text-video/styles"),
            _endpoint("GET", "/api/gen/text-video/voices"),
            _endpoint("GET", "/api/gen/job/{id}"),
        ],
        "task_match": {
            "kind": "script_to_video", "source_page": "text-video",
            "pipeline": "pixelle", "mode": "generate",
        },
        "price_keys": ["text.copy", "image.openai.std", "audio.tts"],
        "smoke_inputs": ["预设中文主题", "竖屏插画模板", "写实商业风格", "公共中文音色"],
        "validation": _validation({
            "mode": "generate", "text": QA_TEXT_VIDEO_TOPIC,
            "template": "1080x1920/image_default.html",
            "style": "realistic_commercial", "source_page": "text-video",
        }),
    }, {
        "key": "text_video.fixed", "name": "完整文案",
        "frontend_selector": '[data-mode="fixed"]',
        "entrypoints": [
            _endpoint("POST", "/api/gen/script_to_video"),
            _endpoint("GET", "/api/gen/text-video/templates"),
            _endpoint("GET", "/api/gen/text-video/styles"),
            _endpoint("GET", "/api/gen/text-video/voices"),
            _endpoint("GET", "/api/gen/job/{id}"),
        ],
        "task_match": {
            "kind": "script_to_video", "source_page": "text-video",
            "pipeline": "pixelle", "mode": "fixed",
        },
        "price_keys": ["image.openai.std", "audio.tts"],
        "smoke_inputs": ["两段预设中文文案", "竖屏插画模板", "写实商业风格", "公共中文音色"],
        "validation": _validation({
            "mode": "fixed", "text": QA_TEXT_VIDEO_SCRIPT,
            "template": "1080x1920/image_default.html",
            "style": "realistic_commercial", "source_page": "text-video",
        }),
    }],
}]


MATRIX_TEMPLATE_FUNCTIONS = [{
    "key": "matrix_template",
    "name": "模板成片",
    "desc": "使用平台素材库与原创视觉模板生成上文字、中素材、下行动文案视频",
    "order": 10,
    "frontend_selector": "#generateBtn",
    "service": "content",
    "flag_keys": ["matrix_template_video"],
    "modes": [{
        "key": "matrix_template.single", "name": "单条模板成片",
        "entrypoints": [
            _endpoint("POST", "/api/gen/matrix-template"),
            _endpoint("GET", "/api/gen/matrix-template/templates"),
            _endpoint("GET", "/api/gen/job/{id}"),
        ],
        "task_match": {"kind": "matrix_template_video"},
        "price_keys": ["video.matrix_template"],
        "smoke_inputs": ["顶部标题", "底部行动文案", "平台素材库", "原生大字模板"],
        "validation": _validation({
            "top_text": "真正拉开差距的，不是工具",
            "bottom_text": "评论区留下关键词，领取完整方案",
            "template_id": "native-bold", "bgm": True,
        }),
    }],
}]


ASSET_FUNCTIONS = [{
    "key": "personal_voice",
    "name": "个人音色",
    "desc": "购买专属槽位、上传样音并生成可复用的个人音色与试听资产",
    "order": 10,
    "frontend_selector": '#cloneVoiceVip',
    "service": "content",
    "flag_keys": ["audio"],
    "dependencies": [{
        "key": "cosyvoice", "role": "VIP 音色复刻", "requirement": "required",
        "credential_source": "env",
    }, {
        "key": "cos", "role": "参考音频与试听存储", "requirement": "required",
        "credential_source": "env",
    }],
    "modes": [{
        "key": "assets.audio.clone_vip", "name": "VIP 音色复刻",
        "entrypoints": [
            _endpoint("POST", "/api/gen/audio/clone-vip"),
            _endpoint("GET", "/api/gen/audio/clone-status?slot_id={slot_id}"),
            _endpoint("GET", "/api/gen/audio/voices"),
        ],
        "evidence_source": "audio_voice_slots",
        "evidence_contract": {
            "acceptance_id_type": "slot_id", "not_applicable": ["billing", "balance"],
        },
        "price_keys": [],
        "smoke_inputs": ["专用测试账号自有音色槽位", "私有短样音", "试听音频"],
        "validation": _validation(
            supported=False,
            blocked_reason="异步复刻尚未绑定统一 run_id；完成前不能用页面轮询冒充八段证据",
        ),
    }],
}]


BROWSER_JOURNEYS = {
    "inspiration": [{
        "key": "inspiration.browse", "name": "浏览、筛选与查看案例",
        "entrypoints": [_endpoint("GET", "/api/admin/public/inspirations")],
        "checks": ["案例列表与分类可见", "详情图片或视频可播放", "提示词可复制、样图可下载"],
    }, {
        "key": "inspiration.like", "name": "收藏案例",
        "entrypoints": [_endpoint("GET", "/api/gen/inspiration/likes"), _endpoint("POST", "/api/gen/inspiration/like")],
        "checks": ["专用测试账号收藏状态可读写", "刷新后状态一致"],
    }, {
        "key": "inspiration.handoff", "name": "做同款预填",
        "entrypoints": [_endpoint("NAVIGATE", "/workbench/banana.html 或 /workbench/video.html")],
        "checks": ["目标功能页正确", "提示词与参考素材预填正确", "生成结果仍由目标页八段验收"],
    }],
    "script": [{
        "key": "script.output.handoff", "name": "脚本结果转视频、同款或图片",
        "entrypoints": [
            _endpoint("POST", "/api/gen/script_to_video"),
            _endpoint("POST", "/api/gen/cinematic"),
            _endpoint("POST", "/api/gen/xiaole_video"),
            _endpoint("POST", "/api/gen/image"),
        ],
        "checks": ["来源固定为 source_page=script", "目标任务归入脚本结果生产模式", "作品与账务由对应生产模式验收"],
    }],
    "short-drama": [{
        "key": "short_drama.autodraft.review", "name": "自动制作与预览复核",
        "entrypoints": [_endpoint("POST", "/api/gen/short-drama/autodraft/jobs")],
        "checks": ["新镜头生产与已有镜头合成可区分", "已有付费镜头不重复扣点", "预览版本可播放"],
    }],
    "canvas": [{
        "key": "canvas.short_drama.node", "name": "短剧节点",
        "entrypoints": [_endpoint("UI", "/workbench/canvas.html")],
        "checks": ["节点可创建与连接", "进入短剧工作区时上下文不丢失"],
    }],
    "assets": [{
        "key": "assets.library.read", "name": "查询、预览与下载资产",
        "entrypoints": [_endpoint("GET", "/api/gen/history"), _endpoint("GET", "/api/gen/audio/assets"), _endpoint("GET", "/api/gen/video/assets")],
        "checks": ["七类资产可切换与搜索", "预览可播放", "单个与批量下载内容有效"],
    }, {
        "key": "assets.library.manage", "name": "收藏、标签、改名与删除",
        "entrypoints": [_endpoint("POST", "/api/gen/asset/batch-delete"), _endpoint("POST", "/api/gen/audio/voice-name"), _endpoint("POST", "/api/gen/video/avatar-name")],
        "checks": ["只修改专用测试账号资产", "刷新后状态一致", "删除操作另行人工确认"],
    }, {
        "key": "assets.voice.slot", "name": "购买个人音色槽位",
        "entrypoints": [_endpoint("POST", "/api/gen/audio/buy-slot"), _endpoint("GET", "/api/gen/audio/slots")],
        "checks": ["槽位增加一次", "扣点流水、余额与槽位价格一致"],
    }],
    "pricing": [{
        "key": "pricing.catalog", "name": "查看实时点数价格",
        "entrypoints": [_endpoint("GET", "/api/gen/pricing")],
        "checks": ["价格分类可渲染", "30 秒刷新不丢失", "请求失败显示明确错误而非旧价格"],
    }],
    "invite": [{
        "key": "invite.dashboard", "name": "查看邀请活动与奖励",
        "entrypoints": [_endpoint("GET", "/api/invite/config"), _endpoint("GET", "/api/invite/dashboard"), _endpoint("GET", "/api/invite/users"), _endpoint("GET", "/api/invite/reward-points")],
        "checks": ["邀请码、人数和奖励记录一致", "分页与刷新可用"],
    }, {
        "key": "invite.poster", "name": "复制邀请信息与下载海报",
        "entrypoints": [_endpoint("GET", "/api/invite/code"), _endpoint("LOCAL", "Clipboard + Canvas/QRCode")],
        "checks": ["复制内容正确", "海报二维码包含当前邀请码"],
    }],
    "tutorials": [{
        "key": "tutorials.playback", "name": "筛选并播放教程",
        "entrypoints": [_endpoint("GET", "/workbench/tutorials.json")],
        "checks": ["教程目录可加载", "MP4 或 iframe 可播放", "失败与超时有明确降级"],
    }],
    "settings": [{
        "key": "settings.profile", "name": "账号资料与昵称",
        "entrypoints": [_endpoint("POST", "/api/auth/profile")],
        "checks": ["服务端保存成功后才提示成功", "刷新与跨设备读取一致"],
    }, {
        "key": "settings.preferences", "name": "主题与通知偏好",
        "entrypoints": [_endpoint("LOCAL", "localStorage")],
        "checks": ["主题作为当前浏览器全局偏好保留", "通知偏好按账号隔离"],
    }, {
        "key": "settings.friends", "name": "好友申请与关系管理",
        "entrypoints": [_endpoint("POST", "/api/auth/friends/request"), _endpoint("POST", "/api/auth/friend-requests/respond"), _endpoint("DELETE", "/api/auth/friends/{username}")],
        "checks": ["只以服务端响应为准", "申请、同意、拒绝和删除状态一致"],
    }, {
        "key": "settings.points", "name": "会员点数赠送",
        "entrypoints": [_endpoint("POST", "/api/auth/points/transfer")],
        "checks": ["仅使用专用测试账号", "双方余额与唯一流水一致"],
    }, {
        "key": "settings.security", "name": "修改密码与退出",
        "entrypoints": [_endpoint("POST", "/api/auth/change_password"), _endpoint("POST", "/api/auth/logout")],
        "checks": ["仅使用可恢复的专用账号", "旧会话失效、新密码可登录"],
    }],
}


_PAGE_DEFS = [
    ("creator-agent", "AI 创作助手", "/workbench/creator-agent.html"),
    ("inspiration", "灵感设计", "/workbench/inspiration.html"),
    ("leads", "平台获客", "/workbench/leads.html"),
    ("collect", "内容爬取", "/workbench/collect.html"),
    ("banana", "图片生成", "/workbench/banana.html"),
    ("video", "视频生成", "/workbench/video.html"),
    ("audio", "音频生成", "/workbench/audio.html"),
    ("script", "文案编导", "/workbench/script.html"),
    ("text-video", "文案成片", "/workbench/text-video.html"),
    ("matrix-template", "模板成片", "/workbench/matrix-template.html"),
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
        "inventory_status": "verified",
        "functions": (
            VIDEO_FUNCTIONS if key == "video"
            else IMAGE_FUNCTIONS if key == "banana"
            else AUDIO_FUNCTIONS if key == "audio"
            else COLLECT_FUNCTIONS if key == "collect"
            else LEADS_FUNCTIONS if key == "leads"
            else SCRIPT_FUNCTIONS if key == "script"
            else TEXT_VIDEO_FUNCTIONS if key == "text-video"
            else MATRIX_TEMPLATE_FUNCTIONS if key == "matrix-template"
            else SHORT_DRAMA_FUNCTIONS if key == "short-drama"
            else CANVAS_FUNCTIONS if key == "canvas"
            else ASSET_FUNCTIONS if key == "assets"
            else []
        ),
        "browser_journeys": deepcopy(BROWSER_JOURNEYS.get(key, [])),
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
        }] if key == "banana" else [{
            "key": "collect.keyword.search", "name": "关键词搜内容",
            "entrypoint": _endpoint("GET", "/api/gen/collect/search"),
            "scope": "付费选片工具，不创建异步任务",
            "price_keys": ["collect.search"],
        }] if key == "collect" else [{
            "key": "leads.crm.update", "name": "线索跟进保存",
            "entrypoint": _endpoint("POST", "/api/gen/leads/crm"),
            "scope": "保存当前用户的意向、跟进状态和备注，不创建生成任务、不扣点",
            "price_keys": [],
        }] if key == "leads" else [{
            "key": "canvas.prompt.reverse", "name": "反推提示词",
            "entrypoint": _endpoint("POST", "/api/gen/reverse"),
            "scope": "同步付费工具；直接返回提示词，不创建异步任务",
            "price_keys": ["image.reverse"],
        }, {
            "key": "canvas.local.edit", "name": "本地画布编辑与协作同步",
            "entrypoint": _endpoint("POST", "/api/auth/canvas/boards/{id}/ops"),
            "scope": "拖拽、连线、撤销和同步不是 AI 生成，不计入渠道健康率",
        }] if key == "canvas" else []),
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
                    "browser_supported": bool((private.get("browser") or {}).get("supported")),
                    "browser_checks": list((private.get("browser") or {}).get("checks") or []),
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
                        "flag_keys": list(dict.fromkeys(
                            list(feature.get("flag_keys") or [])
                            + list(mode.get("flag_keys") or [])
                        )),
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
    return sorted(rules, key=lambda item: len(item[1]), reverse=True)


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
    if source_page not in {"", "video", "banana", "audio", "collect", "leads", "script", "text-video", "canvas", "assets"}:
        return None
    if kind == "image" and source_page not in {"banana", "canvas", "script"}:
        return None
    want = metadata.get("want") or []
    if isinstance(want, str):
        want = [want]
    if kind != "image" and source_page == "banana":
        return None
    if kind == "audio" and source_page != "audio":
        return None
    if kind != "audio" and source_page == "audio":
        return None
    if kind == "copy" and source_page != "script":
        return None
    if kind == "breakdown" and source_page != "script":
        return None
    if kind == "canvas_agent" and source_page != "canvas":
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
        "collect_mode": str(
            metadata.get("collect_mode")
            or next(iter(want), "comments")
        ).strip().lower(),
        "format": str(metadata.get("format") or "").strip().lower(),
        "style": str(metadata.get("style") or "").strip(),
        "source_type": str(metadata.get("source_type") or "").strip().lower(),
        "pipeline": str(metadata.get("pipeline") or "").strip().lower(),
    }
    page_prefix = {"canvas": "canvas.", "script": "script.", "text-video": "text_video."}.get(source_page)
    return next((key for key, rule in TASK_RULES
                 if (not page_prefix or key.startswith(page_prefix))
                 and _matches(actual, rule)), None)


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
                browser = validation.get("browser") or {}
                if browser.get("supported"):
                    if mode["key"] != "image.banana.nb2.reference":
                        raise ValueError("browser E2E pilot is limited to NB2 reference generation")
                    if browser.get("origin") != "https://huangquechuanmei.com":
                        raise ValueError("browser E2E needs the official customer origin")
                    if not browser.get("fixture") or len(browser.get("checks") or []) != 6:
                        raise ValueError("browser E2E needs one private fixture and six checks")
                invalid = set((mode.get("evidence_contract") or {}).get("not_applicable", [])) - {
                    "provider_task", "balance", "billing",
                }
                if invalid:
                    raise ValueError("unknown not-applicable evidence step")
    if len(operation_keys) != len(set(operation_keys)):
        raise ValueError("duplicate operation key")
    return True


validate_registry()
