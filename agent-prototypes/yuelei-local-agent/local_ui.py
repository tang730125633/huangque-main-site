import cgi
import hashlib
import json
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path


SITE_ARG = sys.argv[1] if len(sys.argv) > 1 else None
if SITE_ARG is None:
    print(
        "用法: python local_ui.py <黄雀站点目录> [端口]\n"
        "示例: python local_ui.py E:/AI/data/Huangque/hq-site 8765\n"
        "说明: 需先安装并登录黄雀 CLI(hq_cli), 目录为黄雀站点数据目录。",
        file=sys.stderr,
    )
    sys.exit(2)
SITE = Path(SITE_ARG).resolve()
PREVIEW_ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8765
JOBS = {}
CLI_OFFERS = {}
ATTACHMENTS = {}
CUSTOMER_SESSIONS = {}
RUNS = {}
LOCK = threading.Lock()
MODEL_LOCK = threading.Lock()
CLI_AUTH_LOCK = threading.Lock()
CLI_AUTH_STATE = {"last_attempt_at": 0.0, "last_ok": False}
MODEL_CONFIG = {
    "api_key": os.environ.get("DEEPSEEK_API_KEY", "").strip(),
    "base_url": os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
    "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat").strip() or "deepseek-chat",
}
HQ_CONFIG_DIR = Path(os.environ.get("HQ_CLI_CONFIG_DIR", r"E:\AI\data\Huangque\hq-cli-user")).resolve()
UPLOAD_ROOT = PREVIEW_ROOT / "uploads"
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
RUN_STATE_PATH = PREVIEW_ROOT / "agent-runs.json"
RUN_POLL_SECONDS = 5
MAX_UPLOAD_BYTES = 32 * 1024 * 1024
CUSTOMER_SESSION_TTL = 12 * 60 * 60
MATERIAL_CONSENT_VERSION = "local-material-rights-v1"
UPLOAD_TYPES = {
    ".jpg": ("image", {"image/jpeg"}), ".jpeg": ("image", {"image/jpeg"}),
    ".png": ("image", {"image/png"}), ".webp": ("image", {"image/webp"}),
    ".mp3": ("audio", {"audio/mpeg", "audio/mp3"}), ".wav": ("audio", {"audio/wav", "audio/x-wav"}),
    ".m4a": ("audio", {"audio/mp4", "audio/x-m4a"}), ".aac": ("audio", {"audio/aac"}),
    ".mp4": ("video", {"video/mp4"}), ".mov": ("video", {"video/quicktime"}),
    ".webm": ("video", {"video/webm"}),
}
CLI_DESCRIBE_ALLOWLIST = {
    "director-capability", "director-script-generate", "director-breakdown",
    "director-breakdown-upload", "digital-presenter-capability",
    "digital-ip-text-generate", "text-video-capability", "text-video-generate",
    "text-video-templates", "text-video-styles", "text-video-voices",
    "video-avatars", "voices", "assets", "tasks", "task",
}
CLI_READ_ALLOWLIST = {
    "director-capability", "digital-presenter-capability", "text-video-capability",
    "text-video-templates", "text-video-styles", "text-video-voices",
    "video-avatars", "voices", "assets", "tasks", "task",
}
CLI_QUOTE_ALLOWLIST = {
    "director-script-generate", "director-breakdown",
    "digital-ip-text-generate", "text-video-generate",
}

CAPABILITY_QUERY_PHRASES = (
    "你会什么", "会干什么", "能做什么", "能干什么", "可以做什么",
    "可以干什么", "你能做啥", "你会做啥", "有哪些能力", "有什么能力",
    "有哪些功能", "有什么功能", "功能介绍", "能力介绍", "介绍一下自己", "你是谁",
)


def _normalize_intent_text(value):
    return re.sub(r"[\s,，。！？!?、；;：:]+", "", str(value or "")).lower()


def _is_capability_question(value):
    normalized = _normalize_intent_text(value)
    return normalized in {"功能", "能力"} or any(
        phrase in normalized for phrase in CAPABILITY_QUERY_PHRASES
    )


def _deepseek_request(messages, api_key=None, timeout=45):
    with MODEL_LOCK:
        key = str(api_key or MODEL_CONFIG.get("api_key") or "")
        base_url = MODEL_CONFIG["base_url"]
        model = MODEL_CONFIG["model"]
    if not key:
        return {"ok": False, "error": "model_not_configured", "message": "DeepSeek 尚未连接。"}
    body = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 900,
        "response_format": {"type": "json_object"},
        "stream": False,
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        base_url + "/chat/completions", data=body, method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            message = ((detail.get("error") or {}).get("message") or "DeepSeek 请求失败。")
        except Exception:
            message = "DeepSeek 请求失败，HTTP %d。" % exc.code
        return {"ok": False, "error": "model_http_error", "message": message, "http_status": exc.code}
    except Exception as exc:
        return {"ok": False, "error": "model_network_error", "message": "DeepSeek 连接失败：%s" % exc}
    try:
        content = payload["choices"][0]["message"]["content"]
        value = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return {"ok": False, "error": "invalid_model_output", "message": "DeepSeek 未返回要求的 JSON。"}
    return {
        "ok": True, "value": value, "model": payload.get("model") or model,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "usage": payload.get("usage") if isinstance(payload.get("usage"), dict) else {},
    }


def _deepseek_rewrite(message, history, facts):
    recent = [
        {"role": item.get("role"), "content": str(item.get("content") or "")[:2000]}
        for item in history[-10:]
        if isinstance(item, dict) and item.get("role") in {"user", "assistant"}
    ]
    system = (
        "你是黄雀网站面向顾客的编导总Agent。你负责理解需求、说明可用功能、追问真正缺少的信息并给出明确下一步。"
        "不得提到CLI、命令行、供应商、内部接口或技术实现。不得声称已经上传、扣点、生成或发布，除非事实对象明确说明已经完成。"
        "付费生产必须先报价，再等待顾客点击价格确认按钮。回答简洁自然，避免重复问题。"
        "严格区分输入参数和可推导结果：口播时长不是必填输入。收到文案时应按文案字数、所选声音的实际语速和停顿自动估算，"
        "如果顾客提供的是最终成品口播音频，视频时长直接以音频为准；如果只是音色样本，则仍按文案和该音色语速推算。"
        "只有顾客主动提出必须控制在某个时长内时，才把时长当作约束进行校验，绝不能反问顾客想要多少秒。"
        "顾客发来的口播文案默认视为完整内容，除非文本明确写着未完、待续或要求补写，不能追问是不是完整内容。"
        "已有可用人物图片时不得再询问出镜形象偏好；应采用已有素材形成方案，顾客要求更换时再修改。"
        "系统事实中的missing是权威结果，你只能改写表达，绝不能增加、删除或复活缺失项。script_recorded为true时，"
        "不得声称文案未记录、要求重发文案或询问是否为完整文案。不得使用“稍后会处理”“请留意”等让顾客空等的未来承诺；"
        "当前步骤完成后必须直接说明已经完成的结果，以及顾客现在唯一需要做的下一步。"
        "请只输出一个JSON对象，字段必须是reply、intent、capability、missing、next_step。missing必须是字符串数组。"
    )
    messages = [{"role": "system", "content": system}] + recent + [
        {"role": "user", "content": "顾客当前消息：%s\n系统已确认事实：%s\n请根据事实生成JSON回答。" % (
            message, json.dumps(facts, ensure_ascii=False),
        )}
    ]
    return _deepseek_request(messages)


def _matches_file_signature(extension, data):
    head = data[:16]
    if extension in {".jpg", ".jpeg"}:
        return head.startswith(b"\xff\xd8\xff")
    if extension == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if extension == ".webp":
        return head.startswith(b"RIFF") and head[8:12] == b"WEBP"
    if extension == ".mp3":
        return head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)
    if extension == ".wav":
        return head.startswith(b"RIFF") and head[8:12] == b"WAVE"
    if extension in {".m4a", ".mp4", ".mov"}:
        return len(head) >= 12 and head[4:8] == b"ftyp"
    if extension == ".aac":
        return len(head) >= 2 and head[0] == 0xFF and head[1] & 0xF6 == 0xF0
    if extension == ".webm":
        return head.startswith(b"\x1aE\xdf\xa3")
    return False


def _stage_attachment(handler, owner):
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        return 400, {"detail": "无效的文件长度。"}
    if length <= 0 or length > MAX_UPLOAD_BYTES + 64 * 1024:
        return 413, {"detail": "文件为空或超过 32 MiB。"}
    form = cgi.FieldStorage(
        fp=handler.rfile, headers=handler.headers,
        environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": handler.headers.get("Content-Type", "")},
    )
    field = form["file"] if "file" in form else None
    if field is None or not getattr(field, "filename", None) or not getattr(field, "file", None):
        return 400, {"detail": "没有收到文件。"}
    original_name = Path(str(field.filename)).name[:160]
    extension = Path(original_name).suffix.lower()
    rule = UPLOAD_TYPES.get(extension)
    content_type = str(getattr(field, "type", "") or "").lower()
    if not rule or content_type not in rule[1]:
        return 415, {"detail": "只接受 JPG、PNG、WebP、MP3、WAV、M4A、AAC、MP4、MOV 或 WebM。"}
    data = field.file.read(MAX_UPLOAD_BYTES + 1)
    if not data or len(data) > MAX_UPLOAD_BYTES:
        return 413, {"detail": "文件为空或超过 32 MiB。"}
    if not _matches_file_signature(extension, data):
        return 415, {"detail": "文件内容与扩展名或 MIME 类型不一致。"}
    attachment_id = uuid.uuid4().hex
    target = UPLOAD_ROOT / (attachment_id + extension)
    target.write_bytes(data)
    value = {
        "attachment_id": attachment_id, "name": original_name,
        "kind": rule[0], "size": len(data), "status": "staged",
        "path": str(target), "created_at": int(time.time()), "owner": owner,
    }
    with LOCK:
        ATTACHMENTS[attachment_id] = value
    return 200, {key: value[key] for key in ("attachment_id", "name", "kind", "size", "status")}


def _upload_attachment_to_cli(body, owner):
    attachment_id = str(body.get("attachment_id") or "")
    with LOCK:
        item = ATTACHMENTS.get(attachment_id)
        if not item:
            return 404, {"detail": "附件不存在或服务已重启，请重新选择。"}
        if item.get("owner") != owner:
            return 404, {"detail": "附件不存在或不属于当前账号。"}
        if item.get("status") == "uploaded":
            return 200, {"ok": True, "status": "uploaded", "kind": item["kind"], "name": item["name"]}
        if item.get("status") == "uploading":
            return 409, {"detail": "附件正在上传，禁止重复提交。"}
        item["status"] = "uploading"
        path = item["path"]
        capability = {"image": "image-upload", "video": "video-upload", "audio": "audio-upload"}[item["kind"]]
    if not _ensure_cli_authorized():
        with LOCK:
            item["status"] = "staged"
        return 503, {
            "detail": "当前素材服务暂不可用，请稍后重试或联系工作人员；文件仍安全保留，无需重新选择。",
            "status": "staged",
        }
    result = _run_hq(["run", capability, "--file", path, "--confirm", "--json"], timeout=90)
    payload = result.get("payload") or {}
    if not result.get("ok"):
        with LOCK:
            item["status"] = "staged"
        auth_unavailable = payload.get("error") in {"auth_error", "auth_required"}
        return 401 if auth_unavailable else 502, {
            "detail": (
                "当前素材服务暂不可用，请稍后重试或联系工作人员；文件仍安全保留，无需重新选择。"
                if auth_unavailable else
                "当前未能完成素材上传，请稍后重试；文件仍安全保留，无需重新选择。"
            ),
            "status": "staged", "cli": _cli_public_trace("upload", result, capability),
        }
    cli_result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    with LOCK:
        item["status"] = "uploaded"
        item["cli_upload_id"] = cli_result.get("upload_id")
    return 200, {
        "ok": True, "status": "uploaded", "kind": item["kind"], "name": item["name"],
        "cli": _cli_public_trace("upload", result, capability),
    }


def _ensure_default_attachments():
    image_path = SITE / "workbench" / "assets" / "logo_sparrow.png"
    audio_path = UPLOAD_ROOT / "default-local-test-audio.wav"
    if not audio_path.is_file():
        with wave.open(str(audio_path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(b"\x00\x00" * 16000)
    defaults = [
        {
            "attachment_id": "local-default-image", "name": "默认测试图片.png",
            "kind": "image", "size": image_path.stat().st_size,
            "status": "staged", "path": str(image_path), "created_at": int(time.time()),
            "default": True,
        },
        {
            "attachment_id": "local-default-audio", "name": "默认测试音频.wav",
            "kind": "audio", "size": audio_path.stat().st_size,
            "status": "staged", "path": str(audio_path), "created_at": int(time.time()),
            "default": True,
        },
    ]
    with LOCK:
        for item in defaults:
            ATTACHMENTS[item["attachment_id"]] = item
    return defaults


def _run_hq(arguments, input_value=None, timeout=30):
    env = os.environ.copy()
    env.update({
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "HQ_CLI_CONFIG_DIR": str(HQ_CONFIG_DIR),
    })
    command = [sys.executable, "-m", "hq_cli"] + list(arguments)
    stdin = None if input_value is None else json.dumps(input_value, ensure_ascii=False)
    started = time.monotonic()
    try:
        result = subprocess.run(
            command, input=stdin, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False, "exit_code": None, "duration_ms": int((time.monotonic() - started) * 1000),
            "payload": {"error": "cli_timeout", "message": "CLI 调用超时；未自动重试。"},
        }
    raw = (result.stdout or "").strip() or (result.stderr or "").strip()
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {"error": "invalid_cli_output", "message": "CLI 未返回严格 JSON。"}
    return {
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "payload": payload,
    }


def _ensure_cli_authorized():
    status = _run_hq(["status", "--json"], timeout=20)
    if status.get("ok"):
        with CLI_AUTH_LOCK:
            CLI_AUTH_STATE.update({"last_attempt_at": time.time(), "last_ok": True})
        return True
    now = time.time()
    with CLI_AUTH_LOCK:
        if not CLI_AUTH_STATE.get("last_ok") and now - float(CLI_AUTH_STATE.get("last_attempt_at") or 0) < 60:
            return False
        CLI_AUTH_STATE.update({"last_attempt_at": now, "last_ok": False})
        # Local-only operator recovery. It may use the already signed-in
        # Huangque administrator browser, but no authorization details are
        # returned to the customer page.
        login = _run_hq(["login", "--json"], timeout=180)
        if not login.get("ok"):
            return False
        verified = _run_hq(["status", "--json"], timeout=20)
        CLI_AUTH_STATE["last_ok"] = bool(verified.get("ok"))
        return CLI_AUTH_STATE["last_ok"]


def _cli_public_trace(operation, result, capability=None):
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    return {
        "operation": operation,
        "capability": capability,
        "ok": bool(result.get("ok")),
        "exit_code": result.get("exit_code"),
        "duration_ms": result.get("duration_ms"),
        "schema": payload.get("schema"),
        "cli_version": payload.get("cli_version"),
        "error": payload.get("error"),
        "message": payload.get("message"),
    }


def _cli_catalog_summary():
    result = _run_hq(["capabilities", "--json"], timeout=20)
    payload = result.get("payload") or {}
    capabilities = payload.get("capabilities") if isinstance(payload.get("capabilities"), list) else []
    allowed = [item for item in capabilities if isinstance(item, dict) and item.get("id") in CLI_DESCRIBE_ALLOWLIST]
    return result, [{
        "id": item.get("id"), "name": item.get("name"),
        "side_effect": item.get("side_effect"), "availability": item.get("availability"),
        "confirmation_required": item.get("confirmation_required"),
    } for item in allowed]


def _script_cli_input(history, message):
    user_text = [
        str(item.get("content") or "").strip()
        for item in history[-12:]
        if isinstance(item, dict) and item.get("role") == "user"
    ]
    if message and message not in user_text:
        user_text.append(message)
    prompt_parts = [text for text in user_text if text and not _is_capability_question(text) and text not in {
        "检查CLI", "查看CLI功能", "确认生成",
    }]
    combined = "\n".join(prompt_parts)
    value = {"prompt": combined[:20000] or "请根据当前对话生成营销脚本"}
    if "抖音" in combined:
        value["platform"] = "douyin"
    elif "小红书" in combined:
        value["platform"] = "xiaohongshu"
    elif "视频号" in combined:
        value["platform"] = "channels"
    for duration in (15, 30, 60):
        if "%d秒" % duration in combined or "%ds" % duration in combined.lower():
            value["duration"] = duration
            break
    if "口播" in combined:
        value["style"] = "spoken"
    elif "剧情" in combined or "故事" in combined:
        value["style"] = "story"
    elif "种草" in combined or "推荐" in combined:
        value["style"] = "recommend"
    return value


def _digital_human_quote(customer, attachment_items, script_text):
    image_item = next((item for item in attachment_items if item.get("kind") == "image"), None)
    if not image_item or not script_text:
        return None, "数字人口播方案还不完整，暂时不能报价。", "补齐人物图片和口播文案", None
    if not _ensure_cli_authorized():
        return None, "当前报价服务暂不可用，因此没有取得报价，也没有扣点。请稍后再试或联系工作人员。", "等待报价服务恢复", None

    now = int(time.time())
    upload_id = image_item.get("cli_upload_id")
    if not upload_id or int(image_item.get("cli_upload_expires_at") or 0) <= now + 60:
        upload_result = _run_hq(
            ["run", "image-upload", "--file", image_item["path"], "--confirm", "--json"],
            timeout=90,
        )
        upload_payload = upload_result.get("payload") or {}
        uploaded = upload_payload.get("result") if isinstance(upload_payload.get("result"), dict) else {}
        upload_id = uploaded.get("upload_id")
        if not upload_result.get("ok") or not isinstance(upload_id, str) or not upload_id:
            return None, "当前素材服务暂不可用，因此没有取得报价，也没有扣点。请稍后再试或联系工作人员。", "等待素材服务恢复", _cli_public_trace("upload", upload_result, "image-upload")
        with LOCK:
            image_item["cli_upload_id"] = upload_id
            image_item["cli_upload_expires_at"] = int(uploaded.get("expires_at") or (now + int(uploaded.get("expires_in") or 3600)))

    voices_result = _run_hq(["run", "voices", "--input", "@-", "--json"], input_value={}, timeout=30)
    voices_payload = voices_result.get("payload") or {}
    voices_value = voices_payload.get("result") if isinstance(voices_payload.get("result"), dict) else {}
    voices = voices_value.get("items") if isinstance(voices_value.get("items"), list) else []
    voice_item = next((item for item in voices if isinstance(item, dict) and item.get("scope") == "public" and item.get("voice_key")), None)
    if voice_item is None:
        voice_item = next((item for item in voices if isinstance(item, dict) and item.get("voice_key")), None)
    if not voices_result.get("ok") or not voice_item:
        return None, "当前音色服务暂不可用，因此没有取得报价，也没有扣点。请稍后再试或联系工作人员。", "等待音色服务恢复", _cli_public_trace("voices", voices_result, "voices")

    input_value = {
        "image_upload_id": upload_id,
        "text": str(script_text)[:1000],
        "voice": voice_item["voice_key"],
        "ratio": "9:16",
        "motion": "medium",
        "subtitle": True,
        "subtitle_position": "lower",
        "subtitle_style": "white",
    }
    quote_result = _run_hq(
        ["run", "digital-ip-text-generate", "--input", "@-", "--json"],
        input_value=input_value, timeout=45,
    )
    quote_payload = quote_result.get("payload") or {}
    quote = quote_payload.get("result") if isinstance(quote_payload.get("result"), dict) else {}
    quote_token = quote.get("quote_token")
    cost = quote.get("cost")
    if not quote_result.get("ok") or not isinstance(quote_token, str) or not quote_token or not isinstance(cost, int) or isinstance(cost, bool):
        return None, "当前报价服务暂不可用，因此没有取得报价，也没有扣点。请稍后再试或联系工作人员。", "等待报价服务恢复", _cli_public_trace("quote", quote_result, "digital-ip-text-generate")

    offer_id = uuid.uuid4().hex
    expires_in = int(quote.get("expires_in") or 300)
    with LOCK:
        CLI_OFFERS[offer_id] = {
            "capability": "digital-ip-text-generate",
            "input": input_value,
            "quote_token": quote_token,
            "cost": cost,
            "expires_at": time.time() + expires_in,
            "status": "quoted",
            "owner": customer["username"],
        }
    offer = {
        "offer_id": offer_id,
        "capability": "digital-ip-text-generate",
        "cost": cost,
        "points": quote.get("points"),
        "expires_in": expires_in,
    }
    return offer, "数字人口播方案已经确认，本次报价为 %d 点。请核对价格；只有点击下方价格确认按钮后才会提交生产。" % cost, "核对价格并点击确认按钮，或继续修改内容", _cli_public_trace("quote", quote_result, "digital-ip-text-generate")


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _load_run_state():
    if not RUN_STATE_PATH.is_file():
        return
    try:
        payload = json.loads(RUN_STATE_PATH.read_text(encoding="utf-8"))
        items = payload.get("runs") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return
        with LOCK:
            for item in items:
                if isinstance(item, dict) and str(item.get("job_id") or "").isdigit() and item.get("owner"):
                    RUNS[str(item["job_id"])] = item
    except (OSError, json.JSONDecodeError):
        print("[local-ui] durable run state could not be loaded", flush=True)


def _save_run_state_locked():
    payload = {"version": 1, "runs": sorted(RUNS.values(), key=lambda item: int(item.get("created_at") or 0))}
    temporary = RUN_STATE_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(RUN_STATE_PATH)


def _find_result_url(value):
    if isinstance(value, dict):
        for key in ("video_url", "result_url", "download_url", "file_url", "url"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.startswith(("https://", "http://")):
                return candidate
        for nested in value.values():
            candidate = _find_result_url(nested)
            if candidate:
                return candidate
    elif isinstance(value, list):
        for nested in value:
            candidate = _find_result_url(nested)
            if candidate:
                return candidate
    return None


def _safe_task_error(value):
    text = str(value or "")
    if not text:
        return None
    if "超时" in text or "timed out" in text.lower() or "timeout" in text.lower():
        return "生成服务连接超时"
    if "审核" in text or "合规" in text:
        return "内容未通过生成服务审核"
    if "素材" in text:
        return "素材处理失败"
    return "视频生成失败"


def _update_run_from_task(job_id, task):
    raw_status = str(task.get("status") or "").lower()
    result_value = task.get("result")
    result_url = _find_result_url(result_value)
    if raw_status in {"done", "success", "succeeded", "completed"}:
        status = "succeeded" if result_url else "result_pending"
    elif raw_status in {"error", "failed", "failure", "cancelled", "canceled"}:
        status = "failed"
    else:
        status = "running"
    with LOCK:
        run = RUNS.get(str(job_id))
        if not run:
            return
        run.update({
            "status": status,
            "phase": str(task.get("phase") or raw_status or "running")[:80],
            "updated_at": int(task.get("updated_at") or time.time()),
            "cost": int(task.get("cost") or run.get("cost") or 0),
            "refunded": bool(task.get("refunded")),
            "result_url": result_url,
            "error": _safe_task_error(task.get("error")),
        })
        _save_run_state_locked()


def _run_tracker():
    while True:
        with LOCK:
            pending_ids = [
                job_id for job_id, run in RUNS.items()
                if run.get("status") in {"running", "result_pending", "reconcile_pending"}
            ]
        if pending_ids and _ensure_cli_authorized():
            for job_id in pending_ids:
                result = _run_hq(
                    ["run", "task", "--input", "@-", "--json"],
                    input_value={"job_id": int(job_id)}, timeout=30,
                )
                payload = result.get("payload") or {}
                task = payload.get("result") if isinstance(payload.get("result"), dict) else None
                if result.get("ok") and task:
                    _update_run_from_task(job_id, task)
        time.sleep(RUN_POLL_SECONDS)


def _public_run(run):
    return {
        "job_id": int(run["job_id"]),
        "status": run.get("status") or "running",
        "phase": run.get("phase") or "running",
        "cost": int(run.get("cost") or 0),
        "refunded": bool(run.get("refunded")),
        "result_url": run.get("result_url"),
        "error": run.get("error"),
        "created_at": int(run.get("created_at") or 0),
        "updated_at": int(run.get("updated_at") or 0),
    }


def _new_job(result):
    with LOCK:
        job_id = str(len(JOBS) + 1001)
        JOBS[job_id] = result
    return job_id


def _agent_reply(body):
    prompt = str(body.get("prompt") or "").strip()
    context = body.get("page_context") if isinstance(body.get("page_context"), dict) else {}
    topic = str(context.get("topic") or "").strip() or "当前页面中的选题"
    if prompt == "确认生成":
        history_text = "\n".join(
            str(item.get("content") or "")
            for item in (body.get("history") or [])
            if isinstance(item, dict) and item.get("role") == "user"
        )
        if "东鹏" in history_text:
            topic = "东鹏特饮"
        selling_points = "买三送一" if "买三送一" in history_text else str(context.get("selling_points") or "").strip()
        offer_id = "director-production-localpreview0001"
        input_body = {
            "request_id": offer_id,
            "topic": topic,
            "selling_points": selling_points,
            "style": str(context.get("style") or "口播"),
            "duration": str(context.get("duration") or "30秒"),
            "platform": str(context.get("platform") or "抖音"),
        }
        return {
            "content": "本地预览方案已经冻结。下面显示的是模拟价格确认卡；点击只会生成模拟脚本，不会扣点。",
            "plan": None,
            "production_offer": {
                "offer_id": offer_id,
                "kind": "script",
                "expected_cost": 5,
                "requires_confirmation": True,
                "plan_digest": hashlib.sha256(_json_bytes(input_body)).hexdigest(),
                "quote_token": "local.preview.quote.token.0001",
                "expires_at": int(time.time()) + 3600,
                "page_revision": str(body.get("page_revision") or "00000000"),
                "input": input_body,
                "summary": {
                    "topic": topic,
                    "style": input_body["style"],
                    "duration": input_body["duration"],
                    "platform": input_body["platform"],
                },
            },
        }
    if "东鹏" in prompt or "买三送一" in prompt:
        content = (
            "已识别：主题是东鹏特饮，核心卖点是买三送一。\n"
            "本地预览建议结构：开头直接说优惠，中段说明适合加班、开车或运动场景，结尾强调活动数量有限。\n"
            "如果页面中的选题、卖点、平台和时长无误，请完整回复：确认生成。"
        )
        plan = {
            "page_revision": str(body.get("page_revision") or "00000000"),
            "actions": [
                {"type": "fill_field", "field": "topic", "value": "东鹏特饮", "label": "填入选题"},
                {"type": "fill_field", "field": "selling_points", "value": "买三送一", "label": "填入核心卖点"},
            ],
        }
    else:
        content = (
            "这是本地界面模拟回复。我已经读取当前编导页面状态，但不会调用真实模型或生产服务。\n"
            "你可以测试连续对话、刷新恢复、上传按钮，以及完整回复“确认生成”后的价格确认卡。"
        )
        plan = None
    return {"content": content, "plan": plan, "production_offer": None}


def _is_status_question(message):
    normalized = _normalize_intent_text(message)
    return normalized in {"好了吗", "好了没", "弄好了吗", "准备好了吗", "现在好了吗", "可以了吗"}


def _is_duration_question(message):
    normalized = _normalize_intent_text(message)
    return normalized in {"多长时间", "多久", "多少秒", "视频多长", "口播多长", "时长多少", "预计多久"}


def _script_candidate(message, workflow):
    text = str(message or "").strip()
    explicit = re.search(r"(?:这是|以下是|用这段)?(?:我的)?(?:口播)?文案(?:是|如下)?\s*[：:]\s*(.+)$", text, re.S)
    if explicit and explicit.group(1).strip():
        return explicit.group(1).strip()
    normalized = _normalize_intent_text(text)
    controls = {
        "好了吗", "好了没", "弄好了吗", "准备好了吗", "现在好了吗", "可以了吗",
        "多长时间", "多久", "多少秒", "视频多长", "口播多长", "时长多少", "预计多久",
        "确认生成", "开始", "继续", "直接做", "取消", "停止",
    }
    if normalized in controls or _is_capability_question(text):
        return None
    if any(term in text for term in ("我要制作数字人", "我想做数字人", "数字人口播视频", "帮我做数字人")):
        return None
    if workflow.get("intent") == "digital_human" and workflow.get("awaiting_script") and len(normalized) >= 4:
        return text
    if workflow.get("intent") == "digital_human" and len(normalized) >= 16:
        return text
    return None


def _estimate_spoken_duration(script):
    units = len(re.findall(r"[\u3400-\u9fffA-Za-z0-9]", str(script or "")))
    if not units:
        return 0, 0, 0
    fast = max(1, round(units / 4.5))
    slow = max(fast, round(units / 3.5))
    return units, fast, slow


def _local_chat(body, customer):
    # 第一个性傻瓜确认：顾客不需逐字背“确认生成”，用自然语即可触发本来已在跑的
    # 报价→就地确认卡 流程；仍不真扣点（报价 + 点击按钮才提交）。
    def _wants_confirm(text, missing):
        msg = str(text or "").strip().lower().rstrip("。！？!?")
        if not msg or (missing is not None and missing):
            return False
        affirmative = {
            "可以", "好的", "好", "行", "就用它", "就用这个", "用这个", "用它",
            "生成吧", "开始生成", "直接做", "做成片吧", "就这样", "没问题",
            "可以了", "对，就这个", "就它了", "确认生成", "好，生成", "好了，生成",
            "可以生成", "就按这个生成", "按这个来", "动手吧", "开始吧", "那就开始吧",
        }
        return msg in affirmative or any(msg.startswith(p) for p in
                ("可以，", "好的，", "好，", "行，", "就用", "生成吧", "没问题，", "可以了，", "就这样，"))
    message = str(body.get("message") or "").strip()[:6000]
    history = body.get("history") if isinstance(body.get("history"), list) else []
    requested_attachments = body.get("attachments") if isinstance(body.get("attachments"), list) else []
    with LOCK:
        attachment_items = []
        for item_id in requested_attachments[-8:]:
            item = ATTACHMENTS.get(item_id) if isinstance(item_id, str) else None
            if item and (item.get("default") or item.get("owner") == customer["username"]):
                attachment_items.append(item)
        workflow = customer.setdefault("workflow", {"intent": None, "script_text": None, "awaiting_script": False})
        if any(term in message for term in ("数字人", "口播视频", "照片生成")):
            workflow["intent"] = "digital_human"
        candidate = _script_candidate(message, workflow)
        if candidate:
            workflow["script_text"] = candidate
            workflow["script_updated_at"] = int(time.time())
            workflow["awaiting_script"] = False
        workflow_snapshot = dict(workflow)
    cli_trace = None
    offer = None
    # Intent and parameter extraction must only use customer-authored text.
    # Feeding assistant replies back into this rules engine lets capability
    # descriptions (for example, "数字人") contaminate the next user intent.
    history_text = "\n".join(
        str(item.get("content") or "")
        for item in history[-12:]
        if isinstance(item, dict) and item.get("role") == "user"
    )
    combined = history_text + "\n" + message
    attachment_kinds = {item.get("kind") for item in attachment_items}
    if "image" in attachment_kinds:
        combined += "\n已上传照片"
    if "audio" in attachment_kinds:
        combined += "\n已上传本人录音和音色"
    if "video" in attachment_kinds:
        combined += "\n已上传这个视频.mp4"
    intent = "general"
    capability = "需求理解与能力路由"
    missing = []
    next_step = "继续说明你最终想得到什么结果"

    if any(word in message.lower() for word in ("检查cli", "cli状态", "连接cli")):
        status_result = _run_hq(["status", "--json"], timeout=20)
        catalog_result, allowed = _cli_catalog_summary()
        cli_trace = _cli_public_trace("status", status_result)
        if status_result.get("ok"):
            reply = "当前功能服务可用，已读取到 %d 项可用功能。" % len(allowed)
            next_step = "直接说业务需求，Agent 会先读取能力合同再决定是否调用"
        else:
            reply = "当前部分功能服务暂不可用，请稍后再试或联系工作人员。"
            next_step = "等待功能服务恢复"
        return {
            "reply": reply,
            "analysis": {
                "intent": "service_status", "capability": "执行服务状态检查",
                "missing": [] if status_result.get("ok") else ["可用的功能服务"],
                "next_step": next_step, "speech_problems": [],
                "engine": "local_rules_cli_v2", "cli": cli_trace,
                "catalog_count": len(allowed),
            },
        }

    if any(word in message.lower() for word in ("查看cli功能", "cli有哪些功能", "cli能力")):
        catalog_result, allowed = _cli_catalog_summary()
        cli_trace = _cli_public_trace("capabilities", catalog_result)
        return {
            "reply": "我可以帮助你准备营销脚本、拆解视频、制作数字人口播和文案成片。直接告诉我想得到什么结果，我会检查素材并给出下一步；付费生产会先显示价格，由你点击确认。",
            "analysis": {
                "intent": "capability_catalog", "capability": "可用功能查询",
                "missing": [], "next_step": "选择一项能力或直接描述业务需求",
                "speech_problems": [], "engine": "local_rules_cli_v2", "cli": cli_trace,
                "catalog": allowed,
            },
        }

    if any(word in combined for word in ("脚本", "文案", "口播稿", "东鹏", "卖点")):
        intent, capability = "script", "营销脚本方案"
        if not any(word in combined for word in ("抖音", "小红书", "视频号")):
            missing.append("发布平台")
        if not any(word in combined for word in ("口播", "剧情", "种草")):
            missing.append("表达风格")
        next_step = "补齐缺失项后生成可确认的脚本方案"
    if any(word in combined for word in ("拆解视频", "反推", "分析视频")):
        intent, capability = "breakdown", "视频拆解与提示词反推"
        if not any(word in combined for word in ("http://", "https://", ".mp4", "已上传", "这个视频")):
            missing.append("视频链接或本地视频")
        next_step = "取得视频后拆解结构、镜头和提示词"
    if workflow_snapshot.get("intent") == "digital_human" or any(word in combined for word in ("数字人", "口播视频", "照片生成")):
        intent, capability = "digital_human", "数字人口播方案"
        # A more specific digital-human intent overrides the earlier generic
        # script match. Do not leak script-only questions such as platform or
        # style into this production path.
        missing = []
        if "image" not in attachment_kinds:
            missing.append("本人有权使用的人物照片")
        if "audio" not in attachment_kinds:
            missing.append("音色或本人录音")
        if not workflow_snapshot.get("script_text"):
            missing.append("口播文案")
        consented_ids = set(customer.get("consented_attachment_ids") or [])
        relevant_ids = {item["attachment_id"] for item in attachment_items if item.get("kind") in {"image", "audio", "video"}}
        if relevant_ids and not relevant_ids.issubset(consented_ids):
            missing.append("素材使用授权确认")
        next_step = "补齐素材和授权后准备报价，不直接扣点"
        with LOCK:
            workflow["intent"] = "digital_human"
            workflow["awaiting_script"] = "口播文案" in missing
    if intent == "script" and not missing:
        next_step = "确认脚本方案后获取绑定同一输入的正式报价"
    cli_capability = {
        "script": "director-script-generate",
        "breakdown": "director-breakdown",
        "digital_human": "digital-presenter-capability",
    }.get(intent)
    if cli_capability:
        describe_result = _run_hq(["describe", cli_capability, "--json"], timeout=20)
        cli_trace = _cli_public_trace("describe", describe_result, cli_capability)
    if _is_capability_question(message):
        intent = "capability_overview"
        capability = "能力介绍与功能路由"
        missing = []
        reply = (
            "我能帮你准备营销脚本、视频拆解、数字人口播和文案成片方案，并主动判断还缺哪些信息。\n\n"
            "我可以读取可用功能、执行授权后的查询，并获取无扣点报价。付费生产必须先展示真实价格，再由你点击确认按钮；我不会自动发布或删除内容。\n\n"
            "你可以直接给我一个需求，例如：主题是东鹏特饮，核心卖点买三送一。"
        )
        next_step = "给出一个真实业务需求进行压力测试"
    elif _wants_confirm(message, missing) and intent == "digital_human":
        if missing:
            reply = "现在还不能获取报价，因为方案缺少：%s。请先补齐这些信息，没有提交任务，也没有扣点。" % "、".join(dict.fromkeys(missing))
            next_step = "补齐缺失信息后重新确认生成"
        else:
            offer, reply, next_step, cli_trace = _digital_human_quote(
                customer, attachment_items, workflow_snapshot.get("script_text")
            )
    elif _wants_confirm(message, missing) and intent == "script":
        if missing:
            reply = "现在还不能进入报价确认，因为方案缺少：%s。请先补齐这些信息；本地验收站不会真实扣点或生成。" % "、".join(dict.fromkeys(missing))
            next_step = "补齐缺失参数"
        elif not _ensure_cli_authorized():
            reply = "当前报价服务暂不可用，因此没有取得报价，也没有扣点。请稍后再试或联系工作人员。"
            next_step = "等待报价服务恢复后再次获取报价"
        else:
            cli_input = _script_cli_input(history, message)
            quote_result = _run_hq(
                ["run", "director-script-generate", "--input", "@-", "--json"],
                input_value=cli_input, timeout=45,
            )
            cli_trace = _cli_public_trace("quote", quote_result, "director-script-generate")
            quote_payload = quote_result.get("payload") or {}
            if quote_result.get("ok"):
                quote = quote_payload.get("result") if isinstance(quote_payload.get("result"), dict) else {}
                quote_token = quote.get("quote_token")
                cost = quote.get("cost")
                if isinstance(quote_token, str) and quote_token and isinstance(cost, int) and not isinstance(cost, bool):
                    offer_id = uuid.uuid4().hex
                    with LOCK:
                        CLI_OFFERS[offer_id] = {
                            "capability": "director-script-generate", "input": cli_input,
                            "quote_token": quote_token, "cost": cost,
                            "expires_at": time.time() + int(quote.get("expires_in") or 300),
                            "status": "quoted", "owner": customer["username"],
                        }
                    offer = {
                        "offer_id": offer_id, "capability": "director-script-generate",
                        "cost": cost, "points": quote.get("points"),
                        "expires_in": quote.get("expires_in"),
                    }
                    reply = "本次生成报价为 %d 点。报价已经绑定当前脚本参数；只有你点击下方确认按钮后，才会提交付费任务。" % cost
                    next_step = "核对价格并点击确认按钮，或继续修改需求"
                else:
                    reply = "报价信息不完整；为避免错误扣点，我没有开放确认按钮。"
                    next_step = "检查报价信息"
            else:
                error = quote_payload.get("error")
                if error in {"auth_error", "auth_required"} or (quote_payload.get("details") or {}).get("code") == "cli_unauthorized":
                    reply = "当前报价服务暂不可用，因此没有取得报价，也没有扣点。请稍后再试或联系工作人员。"
                    next_step = "等待报价服务恢复后再次获取报价"
                else:
                    reply = "当前报价服务暂不可用。没有扣点，也没有自动重试；请稍后再试或联系工作人员。"
                    next_step = "检查服务状态或修改输入后重试"
    elif intent == "script":
        known = []
        if "东鹏" in combined:
            known.append("主题：东鹏特饮")
        if "买三送一" in combined:
            known.append("核心卖点：买三送一")
        if missing:
            reply = "已记录%s。为了形成可执行脚本，还缺：%s。请一次性回复这些信息，我再给出完整方案。" % (
                "，".join(known) if known else "你的脚本需求",
                "、".join(dict.fromkeys(missing)),
            )
        else:
            reply = (
                "需求已完整。我建议采用“优惠钩子—使用场景—限时行动”的三段结构：开头直接说买三送一，中段覆盖加班、开车或运动场景，结尾强调活动时间和购买入口。\n\n"
                "如果以上信息无误，请完整回复：确认生成。我会获取无扣点报价；只有你随后点击价格确认按钮才会提交付费任务。"
            )
    elif intent == "breakdown":
        reply = "我识别到你要拆解视频。%s。拿到素材后，我应返回结构、镜头、口播、节奏和可复用提示词，而不是让你跳转页面自己操作。" % (
            "当前还缺视频链接或上传文件" if missing else "视频来源已经具备"
        )
    elif intent == "digital_human":
        if missing:
            reply = "我识别到你要制作数字人口播。当前还缺：%s。请直接在对话框补充这些内容；齐全后我会整理方案和授权摘要，再进入报价与确认。" % "、".join(dict.fromkeys(missing))
            next_step = "在对话框补充：" + "、".join(dict.fromkeys(missing))
        else:
            units, fast_seconds, slow_seconds = _estimate_spoken_duration(workflow_snapshot.get("script_text"))
            duration_text = "预计口播约 %d—%d 秒" % (fast_seconds, slow_seconds) if units else "口播时长将按文案自动计算"
            reply = "文案已经记录，人物图片、音频和素材授权也已齐全，%s。方案已经准备好；如确认使用当前内容，请完整回复：确认生成。系统随后获取报价，只有你点击价格确认按钮才会提交生产。" % duration_text
            next_step = "完整回复“确认生成”以获取报价"
    else:
        reply = "请选择你要使用的功能：AI 写脚本、拆解视频、数字人口播或文案成片。直接回复功能名称和具体需求，我会调用对应能力，并告诉你还缺哪些信息和下一步。"

    authoritative_intent = intent
    authoritative_capability = capability
    authoritative_missing = list(dict.fromkeys(missing))
    authoritative_next_step = next_step
    rule_reply = reply
    forced_reply = None
    if intent == "digital_human" and _is_status_question(message):
        if missing:
            forced_reply = "还没有准备完整，目前只缺：%s。补齐后我会立即整理方案，不需要重复发送已经记录的内容。" % "、".join(authoritative_missing)
        else:
            forced_reply = "已经准备好了。文案、人物图片、音频和素材授权均已记录；现在只差生成确认。请完整回复：确认生成，系统会获取报价，点击价格确认后才提交生产。"
            authoritative_next_step = "完整回复“确认生成”以获取报价"
    elif intent == "digital_human" and _is_duration_question(message):
        script_text = workflow_snapshot.get("script_text")
        units, fast_seconds, slow_seconds = _estimate_spoken_duration(script_text)
        if units:
            forced_reply = "当前文案约 %d 个有效字符，按所选声音的正常语速和停顿，预计口播约 %d—%d 秒，实际时长以合成音频为准。时长由文案自动计算，不需要你再次填写，也不需要重发文案。" % (units, fast_seconds, slow_seconds)
            if not missing:
                forced_reply += "方案已经准备好；如确认生成，请完整回复：确认生成。"
                authoritative_next_step = "完整回复“确认生成”以获取报价"
        else:
            forced_reply = "收到口播文案后，我会根据字数、所选声音的语速和停顿自动估算时长；你不需要预先填写秒数。"

    model_meta = {"connected": False, "used": False, "model": None}
    with MODEL_LOCK:
        model_connected = bool(MODEL_CONFIG.get("api_key"))
        configured_model = MODEL_CONFIG.get("model")
    model_meta.update({"connected": model_connected, "model": configured_model if model_connected else None})
    if model_connected:
        model_result = _deepseek_rewrite(message, history, {
            "rule_reply": reply,
            "intent": authoritative_intent,
            "capability": authoritative_capability,
            "missing": authoritative_missing,
            "next_step": authoritative_next_step,
            "workflow": {
                "script_recorded": bool(workflow_snapshot.get("script_text")),
                "script_units": _estimate_spoken_duration(workflow_snapshot.get("script_text"))[0],
            },
            "attachments": [{"name": item["name"], "kind": item["kind"], "status": item["status"]} for item in attachment_items],
            "quote": ({"cost": offer.get("cost")} if offer else None),
        })
        value = model_result.get("value") if isinstance(model_result.get("value"), dict) else {}
        if model_result.get("ok") and isinstance(value.get("reply"), str) and value.get("reply").strip():
            reply = value["reply"].strip()[:2000]
            if isinstance(value.get("intent"), str) and value["intent"].strip():
                intent = value["intent"].strip()[:80]
            if isinstance(value.get("capability"), str) and value["capability"].strip():
                capability = value["capability"].strip()[:120]
            if isinstance(value.get("next_step"), str) and value["next_step"].strip():
                next_step = value["next_step"].strip()[:240]
            model_meta.update({
                "used": True, "model": model_result.get("model") or configured_model,
                "duration_ms": model_result.get("duration_ms"), "usage": model_result.get("usage") or {},
            })
        else:
            model_meta["error"] = model_result.get("message") or "模型调用失败，已使用本地规则回答。"

    # Model wording is optional; server-confirmed workflow facts are not. The
    # model may never resurrect a missing script or change the action state.
    intent = authoritative_intent
    capability = authoritative_capability
    missing = authoritative_missing
    next_step = authoritative_next_step
    contradiction = bool(workflow_snapshot.get("script_text")) and any(
        phrase in reply for phrase in ("还缺口播文案", "没有正式记录", "再发一次", "确认它就是", "是不是完整文案")
    )
    if forced_reply:
        reply = forced_reply
    elif contradiction or (intent == "digital_human" and not missing and "稍后" in reply):
        reply = rule_reply

    problems = []
    if len(reply) > 320:
        problems.append("回复偏长")
    if "页面" in reply and "跳转" in reply:
        problems.append("可能把操作推回给用户")
    if any(word in reply for word in ("已经生成", "已经上传", "已经扣点")):
        problems.append("可能错误宣称已执行")
    if not any(word in reply for word in ("请", "下一步", "回复", "补齐", "停止", "可以直接", "例如")):
        problems.append("缺少明确下一步")
    return {
        "reply": reply,
        "analysis": {
            "intent": intent,
            "capability": capability,
            "missing": list(dict.fromkeys(missing)),
            "next_step": next_step,
            "speech_problems": problems,
            "engine": "local_rules_cli_v2",
            "cli": cli_trace,
            "attachments": [{
                "attachment_id": item["attachment_id"], "name": item["name"],
                "kind": item["kind"], "status": item["status"],
            } for item in attachment_items],
            "model": model_meta,
        },
        "offer": offer,
    }


def _confirm_cli_offer(body, owner):
    offer_id = str(body.get("offer_id") or "")
    with LOCK:
        stored = CLI_OFFERS.get(offer_id)
        if not stored:
            return 404, {"detail": "报价不存在或服务已重启，请重新获取报价。"}
        if stored.get("owner") != owner:
            return 404, {"detail": "报价不存在或不属于当前账号。"}
        if stored.get("status") != "quoted":
            return 409, {"detail": "该报价已经提交或状态不确定，禁止重复提交。", "status": stored.get("status")}
        if time.time() >= stored.get("expires_at", 0):
            stored["status"] = "expired"
            return 409, {"detail": "报价已过期，请重新获取报价。", "status": "expired"}
        capability = stored["capability"]
        input_value = stored["input"]
        quote_token = stored["quote_token"]
        cost = stored["cost"]
    if not _ensure_cli_authorized():
        return 503, {
            "detail": "当前生产服务暂不可用，没有提交任务，也没有扣点。请稍后再试或联系工作人员。",
            "status": "quoted",
        }
    with LOCK:
        current = CLI_OFFERS.get(offer_id)
        if not current or current.get("status") != "quoted":
            return 409, {"detail": "该报价已经提交或状态不确定，禁止重复提交。", "status": (current or {}).get("status")}
        current["status"] = "submitting"
    confirm_arguments = [
        "run", capability, "--input", "@-", "--confirm",
        "--quote-token", quote_token,
    ]
    # --expected-cost belongs only to the paid Director file-upload contract.
    # Ordinary paid API actions reject it locally before sending any request.
    if capability == "director-breakdown-upload":
        confirm_arguments.extend(["--expected-cost", str(cost)])
    confirm_arguments.append("--json")
    result = _run_hq(confirm_arguments, input_value=input_value, timeout=75)
    payload = result.get("payload") or {}
    error_code = str(payload.get("error") or "")
    uncertain = error_code in {"cli_timeout", "invalid_cli_output", "network_error", "request_timeout"}
    with LOCK:
        current = CLI_OFFERS.get(offer_id)
        if current:
            current["status"] = "submitted" if result.get("ok") else ("submission_unknown" if uncertain else "rejected")
            if result.get("ok") or uncertain:
                current.pop("quote_token", None)
    if not result.get("ok"):
        if not uncertain:
            return 422, {
                "detail": "任务已明确未提交，没有扣点。请重新获取报价后再次确认。",
                "status": "rejected",
            }
        return 502, {
            "detail": "任务提交结果暂时无法确认。为避免重复扣点，系统不会自动重试，请联系工作人员核对。",
            "status": "submission_unknown", "cli": _cli_public_trace("confirm", result, capability),
        }
    public_result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    job_id = public_result.get("job_id")
    if isinstance(job_id, int) and not isinstance(job_id, bool):
        now = int(time.time())
        with LOCK:
            current = CLI_OFFERS.get(offer_id)
            if current:
                current["job_id"] = job_id
            RUNS[str(job_id)] = {
                "job_id": job_id,
                "owner": owner,
                "offer_id": offer_id,
                "capability": capability,
                "status": "running",
                "phase": "submitted",
                "cost": int(public_result.get("cost") or cost),
                "refunded": False,
                "result_url": None,
                "error": None,
                "created_at": now,
                "updated_at": now,
            }
            _save_run_state_locked()
    return 200, {
        "ok": True, "status": "submitted", "capability": capability,
        "job_id": job_id, "cost": public_result.get("cost", cost),
        "next_actions": payload.get("next_actions") or [],
        "cli": _cli_public_trace("confirm", result, capability),
    }


def _customer_session(handler):
    cookie = SimpleCookie()
    try:
        cookie.load(handler.headers.get("Cookie") or "")
    except Exception:
        return None, None
    morsel = cookie.get("hq_local_customer")
    session_id = morsel.value if morsel else ""
    now = int(time.time())
    with LOCK:
        session = CUSTOMER_SESSIONS.get(session_id)
        if session and int(session.get("expires_at") or 0) <= now:
            CUSTOMER_SESSIONS.pop(session_id, None)
            session = None
    return session_id or None, session


class Handler(BaseHTTPRequestHandler):
    server_version = "HuangqueLocalUIPreview/1"

    def log_message(self, fmt, *args):
        print("[local-ui] " + (fmt % args), flush=True)

    def _send_json(self, status, value, headers=None):
        data = _json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for name, header_value in (headers or {}).items():
            self.send_header(name, header_value)
        self.end_headers()
        self.wfile.write(data)

    def _require_customer(self, require_csrf=False):
        session_id, customer = _customer_session(self)
        if not customer:
            self._send_json(401, {"detail": "请先登录黄雀账号后继续。", "code": "customer_login_required"})
            return None
        if require_csrf and not secrets.compare_digest(
            str(self.headers.get("X-CSRF-Token") or ""), str(customer.get("csrf_token") or "")
        ):
            self._send_json(403, {"detail": "当前登录状态已变化，请刷新页面后重试。", "code": "csrf_failed"})
            return None
        customer["session_id"] = session_id
        return customer

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(length, 1024 * 1024))
        value = json.loads(raw.decode("utf-8") or "{}")
        return value if isinstance(value, dict) else {}

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/agent-lab.html")
            self.end_headers()
            return
        if path == "/agent-lab.html":
            target = PREVIEW_ROOT / "agent-lab.html"
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/gen/health":
            return self._send_json(200, {
                "ok": True, "director_agent_enabled": True,
                "director_agent_production_enabled": False,
                "local_preview": True,
            })
        if path == "/api/local-agent/cli/health":
            status_result = _run_hq(["status", "--json"], timeout=20)
            catalog_result, allowed = _cli_catalog_summary()
            return self._send_json(200, {
                "installed": bool(catalog_result.get("ok")),
                "authorized": bool(status_result.get("ok")),
                "capability_count": len(allowed),
                "status": _cli_public_trace("status", status_result),
                "catalog": _cli_public_trace("capabilities", catalog_result),
            })
        if path == "/api/local-agent/material/defaults":
            customer = self._require_customer()
            if not customer:
                return
            with LOCK:
                items = [item for item in ATTACHMENTS.values() if item.get("default")]
            return self._send_json(200, {"items": [{
                key: item[key] for key in ("attachment_id", "name", "kind", "size", "status", "default")
            } for item in items]})
        if path == "/api/local-agent/runs":
            customer = self._require_customer()
            if not customer:
                return
            with LOCK:
                items = [
                    _public_run(run) for run in RUNS.values()
                    if run.get("owner") == customer["username"]
                ]
            items.sort(key=lambda item: item["created_at"], reverse=True)
            return self._send_json(200, {"runs": items[:20]})
        if path == "/api/local-agent/model/status":
            with MODEL_LOCK:
                connected = bool(MODEL_CONFIG.get("api_key"))
                model = MODEL_CONFIG.get("model") if connected else None
            return self._send_json(200, {"connected": connected, "model": model})
        if path == "/api/local-agent/customer/status":
            _, customer = _customer_session(self)
            if not customer:
                return self._send_json(200, {"logged_in": False})
            return self._send_json(200, {
                "logged_in": True,
                "user": {"username": customer["username"], "display_name": customer["display_name"], "points": 999},
                "csrf_token": customer["csrf_token"],
                "material_consent": bool(customer.get("consented_attachment_ids")),
                "consent_version": MATERIAL_CONSENT_VERSION,
            })
        if path == "/api/auth/me":
            customer = self._require_customer()
            if not customer:
                return
            return self._send_json(200, {
                "user": {"username": customer["username"], "display_name": customer["display_name"], "points": 999},
            })
        if path == "/api/gen/pricing":
            return self._send_json(200, {"items": []})
        if path.startswith("/api/gen/points/history"):
            return self._send_json(200, {"items": [], "page": 1, "pages": 1})
        if path.startswith("/api/auth/notifications"):
            return self._send_json(200, {"items": []})
        if path.startswith("/api/gen/assets"):
            return self._send_json(200, {"items": []})
        if path.startswith("/api/gen/audio/voices"):
            return self._send_json(200, {"items": []})
        if path.startswith("/api/gen/video/avatars"):
            return self._send_json(200, {"items": []})
        if path.startswith("/api/gen/job/"):
            job_id = path.rsplit("/", 1)[-1]
            with LOCK:
                result = JOBS.get(job_id)
            if result is None:
                return self._send_json(404, {"detail": "本地模拟任务不存在"})
            return self._send_json(200, {"id": int(job_id), "status": "done", "result": result})
        relative = path.lstrip("/")
        target = (SITE / relative).resolve()
        try:
            target.relative_to(SITE)
        except ValueError:
            return self._send_json(403, {"detail": "forbidden"})
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            return self._send_json(404, {"detail": "not found"})
        data = target.read_bytes()
        media_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if media_type.startswith("text/") or media_type in {"application/javascript", "application/json"}:
            media_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/local-agent/customer/login":
            session_id = secrets.token_urlsafe(32)
            customer = {
                "username": "local-customer", "display_name": "本地测试客户",
                "csrf_token": secrets.token_urlsafe(24),
                "created_at": int(time.time()), "expires_at": int(time.time()) + CUSTOMER_SESSION_TTL,
                "consented_attachment_ids": [],
            }
            with LOCK:
                CUSTOMER_SESSIONS[session_id] = customer
            cookie = "hq_local_customer=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d" % (
                session_id, CUSTOMER_SESSION_TTL,
            )
            return self._send_json(200, {
                "logged_in": True,
                "user": {"username": customer["username"], "display_name": customer["display_name"], "points": 999},
                "csrf_token": customer["csrf_token"],
            }, {"Set-Cookie": cookie})
        if path == "/api/local-agent/customer/logout":
            session_id, _ = _customer_session(self)
            with LOCK:
                if session_id:
                    CUSTOMER_SESSIONS.pop(session_id, None)
            return self._send_json(200, {"logged_in": False}, {
                "Set-Cookie": "hq_local_customer=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0",
            })
        if path == "/api/local-agent/customer/consent":
            customer = self._require_customer(require_csrf=True)
            if not customer:
                return
            body = self._read_json()
            accepted = body.get("accepted") is True
            requested = body.get("attachments") if isinstance(body.get("attachments"), list) else []
            with LOCK:
                allowed = []
                for item_id in requested[-8:]:
                    item = ATTACHMENTS.get(item_id) if isinstance(item_id, str) else None
                    if item and (item.get("default") or item.get("owner") == customer["username"]):
                        allowed.append(item_id)
                customer["consented_attachment_ids"] = sorted(set(allowed)) if accepted else []
                customer["consented_at"] = int(time.time()) if accepted else None
            return self._send_json(200, {
                "accepted": accepted,
                "consent_version": MATERIAL_CONSENT_VERSION,
                "attachment_count": len(allowed) if accepted else 0,
            })
        if path == "/api/local-agent/material/stage":
            customer = self._require_customer(require_csrf=True)
            if not customer:
                return
            status, value = _stage_attachment(self, customer["username"])
            return self._send_json(status, value)
        if path == "/api/local-agent/material/cli-upload":
            customer = self._require_customer(require_csrf=True)
            if not customer:
                return
            status, value = _upload_attachment_to_cli(self._read_json(), customer["username"])
            return self._send_json(status, value)
        if path == "/api/local-agent/model/connect":
            body = self._read_json()
            api_key = str(body.get("api_key") or "").strip()
            if not 16 <= len(api_key) <= 512 or any(char.isspace() for char in api_key):
                return self._send_json(400, {"detail": "请输入有效的 DeepSeek API Key。"})
            test = _deepseek_request([
                {"role": "system", "content": "只输出JSON对象。"},
                {"role": "user", "content": "请输出 {\"ok\":true}。"},
            ], api_key=api_key, timeout=30)
            if not test.get("ok"):
                return self._send_json(401, {"detail": test.get("message") or "DeepSeek 连接失败。"})
            with MODEL_LOCK:
                MODEL_CONFIG["api_key"] = api_key
            return self._send_json(200, {"connected": True, "model": test.get("model") or MODEL_CONFIG["model"]})
        if path == "/api/local-agent/model/disconnect":
            with MODEL_LOCK:
                MODEL_CONFIG["api_key"] = ""
            return self._send_json(200, {"connected": False})
        if path == "/api/local-agent/chat":
            customer = self._require_customer(require_csrf=True)
            if not customer:
                return
            return self._send_json(200, _local_chat(self._read_json(), customer))
        if path == "/api/local-agent/cli/login":
            result = _run_hq(["login", "--json"], timeout=180)
            payload = result.get("payload") or {}
            status = 200 if result.get("ok") else 401
            return self._send_json(status, {
                "ok": bool(result.get("ok")),
                "detail": "授权完成。" if result.get("ok") else (payload.get("message") or "授权未完成。"),
                "cli": _cli_public_trace("login", result),
            })
        if path == "/api/local-agent/cli/confirm":
            customer = self._require_customer(require_csrf=True)
            if not customer:
                return
            status, value = _confirm_cli_offer(self._read_json(), customer["username"])
            return self._send_json(status, value)
        if path == "/api/gen/director_agent":
            body = self._read_json()
            job_id = _new_job(_agent_reply(body))
            return self._send_json(200, {"job_id": int(job_id), "accepted": True, "local_preview": True})
        if path == "/api/gen/director_agent/produce":
            body = self._read_json()
            topic = str((body.get("input") or {}).get("topic") or "东鹏特饮")
            result = {
                "platform": "抖音", "dur": "30秒",
                "scenes": [
                    {"dur": "0-5秒", "scene": "产品与醒目优惠字卡", "line": "东鹏特饮现在买三送一，囤四瓶只算三瓶。"},
                    {"dur": "5-20秒", "scene": "加班、开车、运动三个使用场景", "line": "加班赶进度、长途开车、运动后补状态，随手带上一瓶更方便。"},
                    {"dur": "20-30秒", "scene": "四瓶组合与行动提示", "line": "现在就按%s活动带走，数量有限，先到先得。" % topic},
                ],
            }
            job_id = _new_job(result)
            return self._send_json(200, {"job_id": int(job_id), "accepted": True, "local_preview": True})
        if path in {"/api/auth/logout", "/api/auth/login"}:
            return self._send_json(200, {"ok": True})
        return self._send_json(404, {"detail": "本地预览未实现该接口"})


if __name__ == "__main__":
    _ensure_default_attachments()
    _load_run_state()
    threading.Thread(target=_run_tracker, name="local-run-tracker", daemon=True).start()
    print("Huangque Director Agent local preview: http://%s:%d/workbench/script.html" % (HOST, PORT), flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
