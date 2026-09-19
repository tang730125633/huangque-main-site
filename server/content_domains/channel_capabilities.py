"""渠道切换能力表：哪些任务类型可以换成别的供应商，为什么不能。

背景
----
原来 ``function_registry.operation()`` 里写的是：:

    channel_kind = kind if kind in {"image", "xiaole_video"} else ""

也就是说「能不能切换」由一行硬编码白名单决定，后台只能看到「不能切」，
看不到**为什么**不能切，加了新协议也没人知道该改哪里。

这里把判断改成数据驱动的一张表：每个任务类型显式声明

* ``switchable``  —— 是否已接入托管渠道切换
* ``adapters``    —— 该类型可用的渠道协议（真正能执行它的 channel_runtime 适配器）
* ``reason``      —— 不能切换时的具体原因（后台直接展示给管理员）

**判断标准（不是「有没有供应商」，而是「执行路径是否已接入渠道框架」）**

任务执行在 ``core.py``：

    if payload.get('_channel_binding'):
        result = channel_runtime.run_task(...)   # 托管执行
    else:
        result = HANDLERS[kind](payload)         # 原厂执行

所以一个类型要真正可切换，必须同时满足：

1. ``channel_runtime.generate()`` 支持它的适配器（否则托管执行会失败）；
2. 参数与成品校验（``channel_parameters`` / ``validate_payload``）认识它的 payload；
3. 它自己有稳定的 ``task_match``，能被 ``capture()`` 分类到。

只满足第 3 条就开始放行（例如 ``sora_video``：``core.py`` 已经对它调 ``capture``，
但 ``channel_runtime`` 没有 Sora 适配器），会出现「后台能切、任务却在执行时失败」——
这正是本表要挡住的情况。

新增一个协议的正确做法
----------------------
1. 在 ``channel_runtime.ADAPTERS`` 加协议（请求构造 + 返回解析 + 成品下载）；
2. 在 ``channel_parameters`` 加参数契约；
3. 把这里对应类型的 ``switchable`` 改 True、``adapters`` 填上、``reason`` 清空；
4. 在 ``tests/`` 加密闭环境的请求构造与返回处理测试。
"""

# 每个任务类型：能否切换 / 可用的渠道协议 / 不能切换的原因
CAPABILITIES = {
    # ── 已接入：生图（三种协议都已实现并有成品核验）────────────────────────
    "image": {
        "switchable": True,
        "adapters": ("openai_image", "gemini_image", "lechuang_image"),
        "reason": "",
    },
    # ── 已接入：视频（乐创 / xAI / MiniMax 三条协议）────────────────────────
    "xiaole_video": {
        "switchable": True,
        "adapters": ("lechuang_video", "xai_video", "minimax_h3"),
        "reason": "",
    },

    # ── 有供应商，但执行器还没接进渠道框架：需要新增适配器 ──────────────────
    # ── 已接入：Sora（适配器复用原厂 video_openai，它已支持注入 api_key / api_base）──
    "sora_video": {
        "switchable": True,
        "adapters": ("sora_video",),
        "reason": "",
    },
    "video": {
        "switchable": False,
        "adapters": (),
        "reason": "HeyGen 数字人口播是专用流程（订阅位 / 官方 API + 素材绑定），"
                  "需要先新增 HeyGen 适配器才能切换渠道。",
    },
    "cinematic": {
        "switchable": False,
        "adapters": (),
        "reason": "HeyGen 电影化身与数字人共用形象与素材绑定流程，需要先新增 HeyGen 适配器。",
    },
    "avatar": {
        "switchable": False,
        "adapters": (),
        "reason": "HeyGen 形象生成会写回用户形象归属，需要先新增 HeyGen 适配器并保留归属逻辑。",
    },
    "tryon": {
        "switchable": False,
        "adapters": (),
        "reason": "换装换背景走 WaveSpeed / RunningHub 工作流（多图输入 + 工作流编号），"
                  "需要先新增对应适配器。",
    },
    "audio": {
        "switchable": False,
        "adapters": (),
        "reason": "配音走 CosyVoice 等 TTS 专用流程（含声音复刻与音色槽），"
                  "需要先新增 TTS 适配器。",
    },
    "copy": {
        "switchable": False,
        "adapters": (),
        "reason": "文案走文本模型专用调用，需要先新增文本适配器才能切换供应商。",
    },
    "breakdown": {
        "switchable": False,
        "adapters": (),
        "reason": "素材拆解走多模态理解调用，需要先新增视觉理解适配器。",
    },
    "collect": {
        "switchable": False,
        "adapters": (),
        "reason": "内容采集走 TikHub 专用接口，不是「生成」类供应商，暂不纳入渠道切换。",
    },
    "leads": {
        "switchable": False,
        "adapters": (),
        "reason": "平台获客走固定查询接口，不是「生成」类供应商，暂不纳入渠道切换。",
    },
    "canvas_agent": {
        "switchable": False,
        "adapters": (),
        "reason": "画布 Agent 是规划编排（可能调用多个步骤），没有单一供应商可切换。",
    },

    # ── 组合流程：应按步骤配置，不能一个开关影响整条链路 ────────────────────
    "script_to_video": {
        "switchable": False,
        "adapters": (),
        "reason": "文案成片是组合流水线（文案 → 配音 → 画面 → 合成），"
                  "应按具体步骤分别配置渠道，不能整个功能一次性切换。",
    },
    "matrix_template_video": {
        "switchable": False,
        "adapters": (),
        "reason": "模板成片是组合流水线（文案 → 素材 → 字幕 → 渲染），"
                  "应按具体步骤分别配置渠道。",
    },
    "short_drama_preview": {
        "switchable": False, "adapters": (),
        "reason": "短剧预览是本地渲染合成，没有外部供应商线路。",
    },
    "short_drama_final": {
        "switchable": False, "adapters": (),
        "reason": "短剧成片是本地渲染合成，没有外部供应商线路。",
    },
    "short_drama_remux": {
        "switchable": False, "adapters": (),
        "reason": "短剧转封装是本地处理，没有外部供应商线路。",
    },
    "short_drama_sound_effect": {
        "switchable": False, "adapters": (),
        "reason": "短剧音效走音效供应商专用流程，需先新增对应适配器。",
    },
    "director_agent": {
        "switchable": False, "adapters": (),
        "reason": "编导 Agent 是对话式编排（可能调用多个生成步骤），没有单一供应商可切换。",
    },
}

# 没有 task_match / 不属于「付费生成任务」的项目：明确不适用渠道切换
NO_TASK_KIND_REASON = (
    "这一步是本地处理或组合流程的一部分，没有独立的供应商线路，"
    "不适用渠道切换。"
)


def capability(kind):
    """返回某个任务类型的渠道能力（未知类型按「不适用」处理）。"""
    item = CAPABILITIES.get(str(kind or ""))
    if item:
        return dict(item)
    return {"switchable": False, "adapters": (), "reason": NO_TASK_KIND_REASON}


def is_switchable(kind):
    return bool(capability(kind)["switchable"])


def switchable_kinds():
    return tuple(kind for kind, item in CAPABILITIES.items() if item["switchable"])


def adapters_for(kind):
    return tuple(capability(kind)["adapters"])


def reason_for(kind):
    return capability(kind)["reason"] or NO_TASK_KIND_REASON
