"""微信小程序作品完成订阅消息客户端（仅视频完成事件）。"""
import json
import os
import urllib.parse
import urllib.request

try:
    from . import wechat_virtual_pay as wechat_vpay
except ImportError:
    import wechat_virtual_pay as wechat_vpay


API_URL = "https://api.weixin.qq.com/cgi-bin/message/subscribe/send"
EVENT_TYPE = "work_complete"
PAGE = "pages/assets/assets"
FIELDS = {"thing1": "title", "date2": "time", "thing3": "tip"}


class SubscribeMessageError(RuntimeError):
    def __init__(self, message, code="wechat_error", response=None):
        super().__init__(message)
        self.code = code
        self.response = response or {}


def template_id():
    return (os.environ.get("WX_SUBSCRIBE_WORK_COMPLETE_TEMPLATE_ID") or "").strip()


def configured():
    return bool(template_id())


def public_config():
    return {
        "event_type": EVENT_TYPE,
        "template_id": template_id(),
        "label": "作品完成通知",
        "configured": configured(),
    }


def build_data(title, completed_at, tip):
    return {
        "thing1": {"value": str(title)[:20]},
        "date2": {"value": str(completed_at)[:20]},
        "thing3": {"value": str(tip)[:20]},
    }


def _post_json(url, payload, timeout=12):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8").strip()
            return json.loads(raw) if raw else {}
    except Exception as exc:
        raise SubscribeMessageError("微信订阅消息接口暂时不可用", "network_error") from exc


def send(openid, title, completed_at, tip="视频已生成，可前往资产库查看", tid=""):
    tid = str(tid or template_id()).strip()
    if not tid:
        raise SubscribeMessageError("订阅消息模板未配置", "not_configured")
    openid = str(openid or "").strip()
    if not openid:
        raise SubscribeMessageError("用户尚未绑定微信 OpenID", "missing_openid")
    state = (os.environ.get("WX_SUBSCRIBE_MINIPROGRAM_STATE") or "formal").strip().lower()
    if state not in {"developer", "trial", "formal"}:
        raise SubscribeMessageError("小程序跳转环境配置无效", "bad_config")
    payload = {
        "touser": openid,
        "template_id": tid,
        "page": PAGE,
        "miniprogram_state": state,
        "lang": "zh_CN",
        "data": build_data(title, completed_at, tip),
    }
    def request_with_token(token):
        return _post_json(
            API_URL + "?" + urllib.parse.urlencode({"access_token": token}),
            payload,
        )

    token = wechat_vpay.access_token()
    result = request_with_token(token)
    if int(result.get("errcode") or 0) in wechat_vpay.TOKEN_INVALID_CODES:
        wechat_vpay.invalidate_access_token(token)
        result = request_with_token(wechat_vpay.access_token())
    errcode = int(result.get("errcode") or 0)
    if errcode:
        raise SubscribeMessageError(result.get("errmsg") or "订阅消息发送失败", str(errcode), result)
    return result
