"""Internal quote and price-binding checks for the public HQ CLI gateway."""

import hmac


def _internal_auth(handler, secret):
    supplied = handler.headers.get("X-HQ-Internal-Token") or ""
    return bool(secret) and hmac.compare_digest(supplied, secret)


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
    try:
        request = handler._json_body_strict()
        if not isinstance(request, dict) or set(request) != {"kind", "payload"}:
            raise ValueError("报价请求只允许 kind 和 payload")
        kind, payload = request["kind"], request["payload"]
        if kind == "image":
            from . import image
            payload = image.validate_image_payload(payload)
            payload.pop("short_drama_references", None)
        elif kind == "xiaole_video":
            payload = video.validate_xiaole_video_payload(payload)
        elif kind == "audio":
            payload = audio.validate_audio_payload(payload, user["username"])
        else:
            raise ValueError("CLI 报价仅支持 image、xiaole_video、audio")
        feature_flags.require_enabled(kind)
        handler._send(200, {"kind": kind, "cost": points.cost_of(kind, payload),
                            "points": points.get_points(user["username"])})
    except feature_flags.FeatureDisabled as exc:
        handler._send(503, {"detail": str(exc)})
    except (TypeError, ValueError) as exc:
        handler._send(400, {"detail": str(exc)[:220]})
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
