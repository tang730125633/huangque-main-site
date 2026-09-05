"""运行时配置：黄雀 CLI 路径 + LLM 端点。全部可用环境变量覆盖。"""
import os
import shutil


def _load_dotenv():
    """加载项目根目录的 .env（简单 KEY=VALUE 解析；已存在的环境变量优先）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(root, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and v and k not in os.environ:
                os.environ[k] = v


_load_dotenv()


def _hq_bin() -> str:
    return (
        os.environ.get("HQ_BIN")
        or shutil.which("hq")
        or "/Users/xlzj/.local/bin/hq"
    )


def _llm_config():
    """解析 LLM 配置。优先显式变量，其次自动识别 OpenAI / DeepSeek。

    显式变量（三件套）：
        LLM_API_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY
        LLM_BASE_URL / OPENAI_BASE_URL
        LLM_MODEL / OPENAI_MODEL
    """
    key = os.environ.get("LLM_API_KEY")
    base = os.environ.get("LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    model = os.environ.get("LLM_MODEL") or os.environ.get("OPENAI_MODEL")

    openai_key = os.environ.get("OPENAI_API_KEY")
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY")

    if key:
        base = base or "https://api.openai.com/v1"
        model = model or "gpt-4o-mini"
    elif deepseek_key:
        # 本机项目用 DeepSeek（Agent 运行环境会注入两把 Key，DeepSeek 优先）
        key = deepseek_key
        base = base or "https://api.deepseek.com"
        model = model or "deepseek-chat"
    elif openai_key:
        key = openai_key
        base = base or "https://api.openai.com/v1"
        model = model or "gpt-4o-mini"
    else:
        key = None
        base = base or None
        model = model or "gpt-4o-mini"

    return key, base, model


HQ_BIN = _hq_bin()
LLM_API_KEY, LLM_BASE_URL, LLM_MODEL = _llm_config()
LLM_MODE = "openai" if LLM_API_KEY else "mock"

# 本机有第三方局部代理（如 127.0.0.1:1082 的 MacPacket）时，环境里的
# HTTP(S)_PROXY 会让 LLM 请求绕行代理；该代理偶发挂起连接（只建连不返回数据），
# 导致 Agent 轮次永远卡住。本应用只直连 DeepSeek 等公网 API（直连已验证可用），
# 默认忽略环境代理；确需走代理时设 HQ_USE_AMBIENT_PROXY=1。
if os.environ.get("HQ_USE_AMBIENT_PROXY") != "1":
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"

# 单次 LLM 调用的读超时（秒）：防止代理/服务端挂起把整个会话拖死。
# 超时后调用方会重试一次，仍失败则快速返回可读错误而不是永远转圈。
try:
    LLM_TIMEOUT = float(os.environ.get("HQ_LLM_TIMEOUT", "120"))
except ValueError:
    LLM_TIMEOUT = 120.0

# 主 LLM 显式代理（如 http://127.0.0.1:17897 的 SSH 隧道）。
# 本机 Shadowrocket/MacPacket 等透明代理会劫持 DNS 并挂起 api.deepseek.com 的连接
# （NO_PROXY 挡不住系统级 TUN），显式走 loopback 隧道可绕过劫持稳定出网。
# 不设则直连（环境无劫持时最快）。
MAIN_LLM_PROXY = os.environ.get("HQ_MAIN_LLM_PROXY") or None

# 单轮（主 Agent 或子 Agent）的墙钟总预算（秒）：循环每步都会检查，
# 超预算立即收尾返回，保证任何一轮都不会无限拖长。
try:
    TURN_BUDGET = float(os.environ.get("HQ_TURN_BUDGET", "300"))
except ValueError:
    TURN_BUDGET = 300.0

# 黄雀主站基址：能力结果里的相对媒体路径（如 /api/gen/file/xxx.jpg）补全用
HQ_SITE_BASE = os.environ.get("HQ_SITE_BASE", "https://huangquechuanmei.com")


# ---------------------------------------------------------------------------
# 视觉模型（描述用户上传图片）与子 Agent 按域模型覆盖
# ---------------------------------------------------------------------------

def _vision_config():
    """视觉模型三件套：VISION_* 显式覆盖优先；否则复用主 LLM 端点 + DeepSeek 视觉模型。"""
    key = os.environ.get("VISION_API_KEY") or LLM_API_KEY
    base = os.environ.get("VISION_BASE_URL") or LLM_BASE_URL
    model = os.environ.get("VISION_MODEL") or "deepseek-v4-flash-vision-exp"
    return key, base, model


_MODEL_PROVIDER_PRESETS = {
    # provider → (默认 base_url, 密钥环境变量候选（按序取第一个存在）, 可选代理, create 附加参数)
    "deepseek": ("https://api.deepseek.com", ("DEEPSEEK_API_KEY", "LLM_API_KEY"), None, {}),
    # 国内直连 api.openai.com 不通：默认走本机 SSH 隧道（dapeng-server xray-egress 出境
    # 代理，app.py 启动时自动保持 127.0.0.1:17897 隧道；用 17897 而非 7897 是因为本机
    # MacPacket 透明代理会拦截 7897 这类常见代理端口）；可用 OPENAI_PROXY 覆盖。
    # gpt-5.6-luna 在 /v1/chat/completions 带工具时必须 reasoning_effort=none。
    "openai": ("https://api.openai.com/v1", ("OPENAI_API_KEY",),
               os.environ.get("OPENAI_PROXY", "http://127.0.0.1:17897"),
               {"reasoning_effort": "none"}),
    "openrouter": ("https://openrouter.ai/api/v1", ("OPENROUTER_API_KEY",), None, {}),
}


def _subagent_models():
    """HQ_SUBAGENT_MODELS="domain:provider:model_id[,...]" → {domain: client_cfg}。

    例：HQ_SUBAGENT_MODELS="system:openai:gpt-5.6-luna"
    密钥解析不到的条目跳过（该域回落主 LLM）。domain 用路由表域名（system/digital-human/...）。
    """
    out = {}
    raw = os.environ.get("HQ_SUBAGENT_MODELS", "").strip()
    if not raw:
        return out
    for chunk in raw.split(","):
        parts = [p.strip() for p in chunk.split(":")]
        if len(parts) != 3 or not all(parts):
            continue
        domain, provider, model = parts
        preset = _MODEL_PROVIDER_PRESETS.get(provider)
        if not preset:
            continue
        base, key_names, proxy, create_kwargs = preset
        key = None
        for kn in key_names:
            key = os.environ.get(kn)
            if key:
                break
        if not key:
            continue
        cfg = {"provider": provider, "model": model, "base_url": base, "api_key": key}
        if proxy:
            cfg["proxy"] = proxy
        if create_kwargs:
            cfg["create_kwargs"] = create_kwargs
        out[domain] = cfg
    return out


VISION_API_KEY, VISION_BASE_URL, VISION_MODEL = _vision_config()
SUBAGENT_MODELS = _subagent_models()
