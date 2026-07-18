# -*- coding: utf-8 -*-
"""任务 / 请求路径 → 功能名的【唯一】映射。

## 名字必须和产品里的叫法【逐字一致】

运营在后台看到的名字，要能和用户在界面上点的那个按钮对上号。名字不是这里现编的 ——
每一条都对着前端的实际标签抄：

    site/workbench/video.html   data-function="talking"   → 数字人口播
                                data-function="motion"    → 动作模仿
                                data-function="cinematic" → 电影化身
                                data-function="tryon"     → 换装换背景
                                data-function="grok"      → 果肉视频生成
                                data-function="micro"     → 豆姐视频生成
                                data-function="omni"      → 欧米视频生成
    site/workbench/banana.html  data-engine="gpt"         → gpt-image-2
                                data-engine="seedream"    → Seedream / Seedream Pro
                                data-engine="xiaole"      → 果肉生图
                                data-engine="zelong2"     → 泽龙2生图
                                data-engine="banana"      → Nano Banana 2 / Nano Banana Pro
    各功能页的 <title>          → AI 配音 / 编导 · 文案脚本 / 内容爬取 / 获客

改了前端的标签，就要同步改这里。

## 为什么是一个独立模块

原来这份映射有【两份】拷贝：`admin_api.call_func_name`（运营后台的日志/统计）和
`content_domains.points._history_func_name`（用户的消费明细）。两份已经各自漂移：

    动作模仿    points:「视频 · 动作模仿」   admin:「视频 · 动作模仿 · 线路一(HeyGen)」
                                                  ↑ 线路概念在去线路化(#594)时就删了，motion
                                                    现在只走 WaveSpeed —— 这不是过时，是【错的】
    Seedream    points:「作图 · Seedream」   admin:「作图」（分不出引擎）
    果肉/豆姐/欧米 points:「果肉/微艺视频」     admin:「视频 · 小乐」（三个渠道混成一个）
    电影化身/建形象  两边都【没有】—— 后台日志里原样吐出英文 kind

拿线上近 14 天 1247 条真实任务跑过当时的 admin 映射：**749 条（60%）的功能名是错的或没用的。**

两个服务（huangque-admin / huangque-content）部署在同一个目录，所以两边都能 import 这一份。
**只留一份，别再抄。**

纯 stdlib、零副作用（不碰 DB/env/网络）—— 谁都能安全 import。
"""

# 视频页的三个第三方渠道。名字取自 data-function 的标签。
XIAOLE_CHANNELS = {"grok": "果肉视频生成", "micro": "豆姐视频生成", "omni": "欧米视频生成"}

# 电影化身的三个玩法（#601）。老任务的 payload 里没有 cine_mode，回落到「电影化身」。
CINEMATIC_MODES = {"motion": "动作模仿", "duo": "双人动作模仿", "open": "开放式生成"}

_SIMPLE = {
    "audio": "AI 配音",
    "copy": "编导 · 文案脚本",
    "collect": "内容爬取",
    "breakdown": "爆款拆解",
    "leads": "获客",
    "leadgen": "获客",
    "dl": "无水印下载",
}


def _image_engine(payload):
    """作图引擎。前端 banana.html 的 data-engine → 提交时发的字段：

        gpt      → 不发 provider（后端缺省 openai）
        seedream → provider=seedream (+ variant=pro)
        xiaole   → provider=xiaole
        zelong2  → provider=zelong2
        banana   → 走 /api/gen/banana，发 model=nb2|pro

    zelong（不带 2）是老号池，界面上已经没有入口了，但库里还有存量任务 —— 单独留一个名字，
    别和「泽龙2生图」混成一个。
    """
    model = str(payload.get("model") or "").strip().lower()
    if model == "nb2":
        return "Nano Banana 2"
    if model == "pro":
        return "Nano Banana Pro"
    provider = str(payload.get("provider") or "openai").strip().lower()
    if provider == "openai":
        return "gpt-image-2"
    if provider == "seedream":
        return "Seedream Pro" if str(payload.get("variant") or "").lower() == "pro" else "Seedream"
    if provider == "xiaole":
        return "果肉生图"
    if provider == "zelong2":
        return "泽龙2生图"
    if provider == "zelong":
        return "泽龙"          # 老号池，界面已无入口，存量任务还在
    return ""


def func_name(kind, payload=None):
    """(kind, payload) → 功能名。

    payload 可能是被截断后用正则兜底捞出来的（日志那条路只取 payload 前缀，因为整条能有几百 KB
    的 base64），所以取字段一律要宽容：拿不到就回落到功能本身的名字，不能崩、也不能吐英文。
    """
    kind = kind or "unknown"
    payload = payload or {}

    if kind == "image":
        engine = _image_engine(payload)
        return "作图 · " + engine if engine else "作图"

    if kind == "video":
        mode = str(payload.get("mode") or "").strip().lower()
        if mode == "text":
            return "数字人口播 · 文案"
        if mode == "audio":
            return "数字人口播 · 音频"
        if mode == "motion":
            return "动作模仿"      # 去线路化(#594)后只走 WaveSpeed，不再有线路之分
        return "视频生成"

    if kind == "cinematic":
        sub = CINEMATIC_MODES.get(str(payload.get("cine_mode") or "").strip().lower())
        return "电影化身 · " + sub if sub else "电影化身"

    if kind == "avatar":
        return "创建数字人形象"

    if kind == "sora_video":
        model = str(payload.get("model") or "sora-2").strip().lower()
        return "Sora 2 Pro 视频" if model == "sora-2-pro" else "Sora 2 视频"

    if kind == "xiaole_video":
        return XIAOLE_CHANNELS.get(str(payload.get("channel") or "").strip().lower(),
                                   "果肉/豆姐/欧米视频")

    if kind == "tryon":
        # 换装【仍然】有线路之分（线路一 RunningHub / 线路二 WaveSpeed），前端也还给用户选。
        # 只有动作模仿的线路被删了 —— 别顺手把这里也一起删了。
        line2 = str(payload.get("line") or "1").strip() == "2"
        return "换装换背景 · " + ("线路二(WaveSpeed)" if line2 else "线路一(RunningHub)")

    if kind == "collect":
        if str(payload.get("keyword") or "").strip():
            return "内容爬取 · 关键词搜索"
        if str(payload.get("url") or "").strip():
            return "内容爬取 · 贴链接"
        return "内容爬取"

    return _SIMPLE.get(kind, kind)


# 请求路径 → 功能名。**顺序即优先级**，长前缀必须排在短前缀【前面】：
# /api/gen/video/assets（读历史）要是排在 /api/gen/video（提交）后面，就会被标成「提交」——
# 统计里凭空多出一堆根本没发生过的提交。test_no_prefix_is_shadowed_by_a_shorter_one_before_it
# 会自动查全表，防止以后有人往中间插一条把后面的吃掉。
PATH_FUNCS = [
    ("/api/gen/file/", "取结果文件"),
    ("/api/gen/video/assets", "视频 · 读历史"),
    ("/api/gen/video/avatars", "数字人形象 · 读列表"),
    ("/api/gen/video/avatar-", "数字人形象 · 改名/删除"),
    ("/api/gen/video/batch", "数字人口播 · 批量提交"),
    ("/api/gen/sora_video", "Sora 2 限时测试 · 提交"),
    ("/api/gen/xiaole_video", "果肉/豆姐/欧米视频 · 提交"),
    ("/api/gen/cinematic", "电影化身 · 提交"),
    ("/api/gen/avatar", "创建数字人形象 · 提交"),
    ("/api/gen/image", "作图 · 提交"),
    ("/api/gen/banana", "作图 · 提交"),      # Nano Banana 走的是这条独立路由
    ("/api/gen/video", "视频 · 提交"),
    ("/api/gen/audio", "AI 配音 · 提交"),
    ("/api/gen/tryon", "换装换背景 · 提交"),
    ("/api/gen/copy", "编导 · 文案脚本 · 提交"),
    ("/api/gen/collect", "内容爬取 · 提交"),
    ("/api/gen/breakdown", "爆款拆解 · 提交"),
    ("/api/gen/asset", "资产库"),
    ("/api/gen/dl", "无水印下载"),
    ("/api/gen/history", "历史记录"),
    ("/api/gen/leadgen", "获客"),
    ("/api/auth/login", "登录"),
    ("/api/auth/logout", "退出登录"),
    ("/api/auth/me", "登录态校验"),
    ("/api/auth/", "账号服务"),
    ("/api/admin/", "运营后台"),
    ("/api/claim", "采集 worker 轮询"),
    ("/api/keywords", "关键词库"),
]


def path_func(path):
    if "/health" in path:
        return "健康检查"
    for prefix, name in PATH_FUNCS:
        if path.startswith(prefix):
            return name
    return ""
