"""Internal quote and price-binding checks for the public HQ CLI gateway."""

import hmac
import urllib.parse

from . import cli_uploads, pricing


def _internal_auth(handler, secret):
    supplied = handler.headers.get("X-HQ-Internal-Token") or ""
    return bool(secret) and hmac.compare_digest(supplied, secret)


def _require_ready_avatar(video, username, avatar_id, cinematic=False):
    avatar = video.get_video_avatar(username, avatar_id)
    if avatar.get("status") != "ready" or not avatar.get("image_file"):
        raise ValueError("所选人物形象尚未就绪")
    if cinematic and not avatar.get("provider_avatar_id"):
        raise ValueError("所选电影化身尚未就绪")
    return avatar


def _strict_payload(payload, allowed, required=()):
    if not isinstance(payload, dict):
        raise ValueError("payload 必须是 JSON 对象")
    unknown = sorted(set(payload) - set(allowed))
    if unknown:
        raise ValueError("不支持的参数：" + unknown[0])
    missing = [key for key in required if key not in payload]
    if missing:
        raise ValueError("缺少参数：" + missing[0])


def _text(value, field, minimum=0, maximum=120):
    if not isinstance(value, str):
        raise ValueError(field + " 必须是字符串")
    value = value.strip()
    if not minimum <= len(value) <= maximum or any(ord(char) < 32 for char in value):
        raise ValueError(field + " 长度或内容不合法")
    return value


def _number(value, field, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError("%s 必须是 %d-%d 的整数" % (field, minimum, maximum))
    return value


def _collect_payload(payload):
    _strict_payload(payload, {"url", "want"}, ("url", "want"))
    url = _text(payload["url"], "url", 1, 2048)
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        raise ValueError("url 格式不合法")
    allowed = ("douyin.com", "iesdouyin.com", "xiaohongshu.com", "xhslink.com", "xhslink.cn",
               "bilibili.com", "b23.tv")
    channels_share = (parsed.scheme == "https" and host == "weixin.qq.com" and port in (None, 443)
                      and parsed.path.startswith("/sph/") and parsed.path[len("/sph/"):].isalnum())
    if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
            or port not in (None, 80, 443)
            or not (channels_share or any(
                host == suffix or host.endswith("." + suffix) for suffix in allowed))):
        raise ValueError("url 仅支持抖音、小红书、视频号或 B 站公开链接")
    want = payload["want"]
    if not isinstance(want, list) or len(want) != 1 or want[0] not in {"comments", "video", "transcript"}:
        raise ValueError("want 仅支持 comments、video 或 transcript 中的一项")
    return {"url": url, "want": list(want)}


def _collect_search_payload(payload):
    _strict_payload(payload, {"platform", "keyword", "page"}, ("platform", "keyword", "page"))
    platform = _text(payload["platform"], "platform", 1, 20)
    if platform not in {"douyin", "xhs"}:
        raise ValueError("platform 仅支持 douyin 或 xhs")
    return {
        "platform": platform,
        "keyword": _text(payload["keyword"], "keyword", 1, 120),
        "page": _number(payload["page"], "page", 1, 50),
    }


def _leads_payload(payload):
    _strict_payload(payload, {"keyword", "platforms", "count", "pages", "channels_targets"},
                    ("keyword", "platforms", "count", "pages", "channels_targets"))
    raw_platforms = payload["platforms"]
    if not isinstance(raw_platforms, list) or not 1 <= len(raw_platforms) <= 3:
        raise ValueError("platforms 必须是包含 1-3 项的平台数组")
    platforms = []
    for item in raw_platforms:
        item = _text(item, "platforms", 1, 20)
        if item not in {"douyin", "xhs", "channels"} or item in platforms:
            raise ValueError("platforms 包含不支持或重复的平台")
        platforms.append(item)
    raw_targets = payload["channels_targets"]
    if not isinstance(raw_targets, list) or len(raw_targets) > 20:
        raise ValueError("channels_targets 必须是最多 20 项的数组")
    targets = [_text(item, "channels_targets", 1, 120) for item in raw_targets]
    if len(targets) != len(set(targets)):
        raise ValueError("channels_targets 不能重复")
    keyword = _text(payload["keyword"], "keyword", 0, 120)
    if any(platform != "channels" for platform in platforms) and not keyword:
        raise ValueError("抖音或小红书获客必须提供 keyword")
    if "channels" in platforms and not targets:
        raise ValueError("视频号获客必须提供 channels_targets")
    return {
        "keyword": keyword, "platforms": platforms,
        "count": _number(payload["count"], "count", 1, 30),
        "pages": _number(payload["pages"], "pages", 1, 3),
        "channels_targets": targets,
    }


def handle_image_upload(handler, path, verify, must_change_password, secret):
    if path != "/api/gen/cli/image-upload":
        return False
    if not _internal_auth(handler, secret):
        handler._send(403, {"detail": "forbidden"})
        return True
    user = verify(handler._token())
    if not user:
        handler._send(401, {"detail": "未登录或登录已过期"})
        return True
    if must_change_password(user):
        handler._send(403, {"detail": "请先修改初始密码"})
        return True
    try:
        if handler.headers.get("Transfer-Encoding"):
            raise ValueError("图片上传必须提供 Content-Length")
        length = int(handler.headers.get("Content-Length") or 0)
        content_type = (handler.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        result = cli_uploads.store_image(
            handler.rfile, length, user["username"], content_type,
            handler.headers.get("X-HQ-Image-SHA256"),
        )
        handler._send(200, result)
    except ValueError as exc:
        handler._send(400, {"detail": str(exc)[:220], "code": "invalid_image_upload"})
    except OSError:
        handler._send(500, {"detail": "图片暂时无法保存", "code": "image_upload_failed"})
    return True


def handle_video_upload(handler, path, verify, must_change_password, secret):
    if path != "/api/gen/cli/video-upload":
        return False
    if not _internal_auth(handler, secret):
        handler._send(403, {"detail": "forbidden"})
        return True
    user = verify(handler._token())
    if not user:
        handler._send(401, {"detail": "未登录或登录已过期"})
        return True
    if must_change_password(user):
        handler._send(403, {"detail": "请先修改初始密码"})
        return True
    try:
        if handler.headers.get("Transfer-Encoding"):
            raise ValueError("视频上传必须提供 Content-Length")
        length = int(handler.headers.get("Content-Length") or 0)
        content_type = (handler.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        result = cli_uploads.store_video(
            handler.rfile, length, user["username"], content_type,
            handler.headers.get("X-HQ-Video-SHA256"),
        )
        handler._send(200, result)
    except ValueError as exc:
        handler._send(400, {"detail": str(exc)[:220], "code": "invalid_video_upload"})
    except OSError:
        handler._send(500, {"detail": "视频暂时无法保存", "code": "video_upload_failed"})
    return True


def handle_audio_upload(handler, path, verify, must_change_password, secret):
    if path != "/api/gen/cli/audio-upload":
        return False
    if not _internal_auth(handler, secret):
        handler._send(403, {"detail": "forbidden"})
        return True
    user = verify(handler._token())
    if not user:
        handler._send(401, {"detail": "未登录或登录已过期"})
        return True
    if must_change_password(user):
        handler._send(403, {"detail": "请先修改初始密码"})
        return True
    try:
        if handler.headers.get("Transfer-Encoding"):
            raise ValueError("音频上传必须提供 Content-Length")
        length = int(handler.headers.get("Content-Length") or 0)
        content_type = (handler.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        result = cli_uploads.store_audio(
            handler.rfile, length, user["username"], content_type,
            handler.headers.get("X-HQ-Audio-SHA256"),
        )
        handler._send(200, result)
    except ValueError as exc:
        handler._send(400, {"detail": str(exc)[:220], "code": "invalid_audio_upload"})
    except OSError:
        handler._send(500, {"detail": "音频暂时无法保存", "code": "audio_upload_failed"})
    return True


def handle_quote(handler, path, verify, must_change_password, is_shutting_down,
                 feature_flags, points, audio, video, secret):
    if path != "/api/gen/cli/quote":
        return False
    if not _internal_auth(handler, secret):
        handler._send(403, {"detail": "forbidden"})
        return True
    user = verify(handler._token())
    if not user:
        handler._send(401, {"detail": "未登录或登录已过期"})
        return True
    if must_change_password(user):
        handler._send(403, {"detail": "请先修改初始密码"})
        return True
    if is_shutting_down():
        handler._send(503, {"detail": "服务正在更新，请稍后重新报价", "code": "shutting_down"})
        return True
    temporary_reference_files = []
    try:
        request = handler._json_body_strict()
        if not isinstance(request, dict) or set(request) != {"kind", "payload"}:
            raise ValueError("报价请求只允许 kind 和 payload")
        kind, payload = request["kind"], request["payload"]
        if kind == "image":
            from . import image
            payload = cli_uploads.expand_image_payload(payload, user["username"])
            payload = image.validate_image_payload(payload)
            payload.pop("short_drama_references", None)
        elif kind == "xiaole_video":
            payload = cli_uploads.expand_image_payload(payload, user["username"])
            payload = video.validate_xiaole_video_payload(payload, user["username"])
        elif kind == "sora_video":
            payload = cli_uploads.expand_image_payload(payload, user["username"])
            payload = video.validate_sora_video_payload(payload)
        elif kind == "audio":
            payload = audio.validate_audio_payload(payload, user["username"])
        elif kind == "video":
            if not isinstance(payload, dict):
                raise ValueError("请求体不是合法 JSON")
            if str(payload.get("mode") or "") == "lipsync":
                payload = video.validate_video_payload(payload, user["username"])
            else:
                image_upload = bool(payload.get("image_upload_id"))
                audio_upload = bool(payload.get("audio_upload_id"))
                if payload.get("image_data") and not image_upload:
                    raise ValueError("CLI 数字人口播仅支持本人形象或私密上传照片")
                if payload.get("audio_data") and not audio_upload:
                    raise ValueError("CLI 数字人口播仅支持本人资产音频或私密上传音频")
                payload = cli_uploads.expand_talking_media_payload(payload, user["username"])
                if payload.get("bgm_data"):
                    raise ValueError("CLI 数字人口播不支持 BGM")
                payload = video.validate_video_payload(payload, user["username"])
                if payload.get("avatar_id"):
                    _require_ready_avatar(video, user["username"], payload["avatar_id"])
                if payload["mode"] == "text":
                    audio.resolve_audio_provider_voice(user["username"], payload["voice"])
                elif not payload.get("audio_file") and not payload.get("audio_data"):
                    raise ValueError("音频驱动数字人缺少本人音频")
        elif kind == "video_batch":
            payloads = video.validate_video_batch_payload(payload, user["username"])
            for item in payloads:
                _require_ready_avatar(video, user["username"], item["avatar_id"])
            audio.resolve_audio_provider_voice(user["username"], payloads[0]["voice"])
            payload = payloads
        elif kind == "cinematic":
            if not isinstance(payload, dict):
                raise ValueError("请求体不是合法 JSON")
            payload = cli_uploads.expand_role_media_payload(payload, user["username"])
            payload = video.validate_cinematic_payload(
                payload, user["username"], temporary_reference_files)
            for avatar_id in payload["avatar_ids"]:
                _require_ready_avatar(video, user["username"], avatar_id, cinematic=True)
        elif kind == "tryon":
            payload = cli_uploads.expand_role_media_payload(payload, user["username"])
            payload = video.validate_tryon_payload(payload)
        elif kind == "collect":
            payload = _collect_payload(payload)
        elif kind == "collect_search":
            payload = _collect_search_payload(payload)
        elif kind == "leads":
            payload = _leads_payload(payload)
        else:
            raise ValueError("CLI 报价不支持该生成类型")
        feature_flags.require_enabled(
            "banana" if kind == "image" and payload.get("provider") == "banana"
            else "video" if kind == "video_batch"
            else "collect" if kind == "collect_search" else kind
        )
        cost = (sum(points.cost_of("video", item) for item in payload)
                if kind == "video_batch" else pricing.get_price("collect.search")
                if kind == "collect_search" else points.cost_of(kind, payload))
        handler._send(200, {"kind": kind, "cost": cost,
                            "points": points.get_points(user["username"])})
    except feature_flags.FeatureDisabled as exc:
        handler._send(503, {"detail": str(exc)})
    except (TypeError, ValueError) as exc:
        handler._send(400, {"detail": str(exc)[:220]})
    finally:
        if temporary_reference_files:
            video._cleanup_cinematic_reference_files(temporary_reference_files)
    return True


def reject_changed_cost(handler, cost, secret):
    expected = handler.headers.get("X-HQ-Expected-Cost")
    if expected is None:
        return False
    if not _internal_auth(handler, secret):
        handler._send(403, {"detail": "forbidden"})
        return True
    try:
        expected = int(expected)
    except (TypeError, ValueError):
        handler._send(400, {"detail": "expected cost is invalid", "code": "invalid_expected_cost"})
        return True
    if cost != expected:
        handler._send(409, {"detail": "生成价格已变化，请重新报价", "code": "quote_cost_changed",
                            "quoted_cost": expected, "current_cost": cost})
        return True
    return False
