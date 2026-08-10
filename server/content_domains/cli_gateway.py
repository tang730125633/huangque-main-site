"""Internal quote and price-binding checks for the public HQ CLI gateway."""

import hmac

from . import cli_uploads


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
            payload = video.validate_xiaole_video_payload(payload)
        elif kind == "sora_video":
            payload = cli_uploads.expand_image_payload(payload, user["username"])
            payload = video.validate_sora_video_payload(payload)
        elif kind == "audio":
            payload = audio.validate_audio_payload(payload, user["username"])
        elif kind == "video":
            if not isinstance(payload, dict):
                raise ValueError("请求体不是合法 JSON")
            if payload.get("image_data") or not payload.get("avatar_id"):
                raise ValueError("CLI 数字人口播第一阶段仅支持本人形象 avatar_id")
            if payload.get("audio_data") or payload.get("bgm_data"):
                raise ValueError("CLI 数字人口播第一阶段仅支持本人资产音频且不支持 BGM")
            payload = video.validate_video_payload(payload, user["username"])
            _require_ready_avatar(video, user["username"], payload["avatar_id"])
            if payload["mode"] == "text":
                audio.resolve_audio_provider_voice(user["username"], payload["voice"])
            elif not payload.get("audio_file"):
                raise ValueError("CLI 现成音频生成仅支持本人资产 audio_file")
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
        else:
            raise ValueError("CLI 报价不支持该生成类型")
        feature_flags.require_enabled(
            "banana" if kind == "image" and payload.get("provider") == "banana"
            else "video" if kind == "video_batch" else kind
        )
        cost = (sum(points.cost_of("video", item) for item in payload)
                if kind == "video_batch" else points.cost_of(kind, payload))
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
