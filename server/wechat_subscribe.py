#!/usr/bin/env python3
"""微信小程序订阅消息客户端。

模板 ID 与字段映射由服务器环境变量提供，不把运营后台配置写死在代码里。
WX_SUBSCRIBE_TEMPLATES_JSON 示例：

{
  "work_complete": {
    "template_id": "TEMPLATE_ID",
    "page": "pages/assets/assets",
    "label": "作品完成通知",
    "fields": {"thing1": "title", "phrase2": "status", "date3": "time"}
  },
  "recharge_credited": {
    "template_id": "TEMPLATE_ID",
    "page": "pages/recharge/recharge",
    "label": "充值到账通知",
    "fields": {"number1": "points", "amount2": "amount", "date3": "time"}
  }
}
"""
import json
import os
import re
import urllib.parse
import urllib.request

try:
    from . import wechat_virtual_pay as wechat_vpay
except ImportError:
    import wechat_virtual_pay as wechat_vpay


API_URL = "https://api.weixin.qq.com/cgi-bin/message/subscribe/send"
EVENTS = {
    "work_complete": {
        "label": "作品完成通知",
        "page": "pages/assets/assets",
        "values": {"title", "status", "time", "job_id"},
    },
    "recharge_credited": {
        "label": "充值到账通知",
        "page": "pages/recharge/recharge",
        "values": {"points", "amount", "time", "order_id"},
    },
}
FIELD_RE = re.compile(
    r"^(thing|number|letter|symbol|character_string|time|date|amount|"
    r"phone_number|car_number|name|phrase|enum)\d+$"
)


class SubscribeMessageError(RuntimeError):
    def __init__(self, message, code="wechat_error", response=None):
        super().__init__(message)
        self.code = code
        self.response = response or {}


def _raw_config():
    raw = (os.environ.get("WX_SUBSCRIBE_TEMPLATES_JSON") or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except Exception as exc:
        raise SubscribeMessageError("订阅消息模板 JSON 无效", "bad_config") from exc
    if not isinstance(value, dict):
        raise SubscribeMessageError("订阅消息模板配置必须是对象", "bad_config")
    return value


def event_config(event_type):
    event_type = str(event_type or "").strip()
    base = EVENTS.get(event_type)
    if not base:
        return None
    item = _raw_config().get(event_type) or {}
    if not isinstance(item, dict):
        raise SubscribeMessageError("订阅消息事件配置无效", "bad_config")
    template_id = str(item.get("template_id") or "").strip()
    fields = item.get("fields") or {}
    if not isinstance(fields, dict):
        raise SubscribeMessageError("订阅消息 fields 必须是对象", "bad_config")
    clean_fields = {}
    for field, semantic in fields.items():
        field = str(field or "").strip()
        semantic = str(semantic or "").strip()
        if not FIELD_RE.match(field) or semantic not in base["values"]:
            raise SubscribeMessageError("订阅消息字段映射无效: %s" % field, "bad_config")
        clean_fields[field] = semantic
    return {
        "event_type": event_type,
        "template_id": template_id,
        "label": str(item.get("label") or base["label"]).strip()[:30],
        "page": str(item.get("page") or base["page"]).strip(),
        "fields": clean_fields,
        "configured": bool(template_id and clean_fields),
    }


def configured_events():
    result = []
    for event_type in EVENTS:
        config = event_config(event_type)
        if config and config["configured"]:
            result.append(config)
    return result


def public_config():
    return [{
        "event_type": item["event_type"],
        "template_id": item["template_id"],
        "label": item["label"],
    } for item in configured_events()]


def build_data(config, values):
    values = values or {}
    data = {}
    for field, semantic in config["fields"].items():
        value = values.get(semantic)
        if value is None:
            raise SubscribeMessageError("订阅消息缺少字段: %s" % semantic, "bad_data")
        data[field] = {"value": str(value)}
    return data


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


def send(event_type, openid, values):
    config = event_config(event_type)
    if not config or not config["configured"]:
        raise SubscribeMessageError("订阅消息模板未配置", "not_configured")
    openid = str(openid or "").strip()
    if not openid:
        raise SubscribeMessageError("用户尚未绑定微信 OpenID", "missing_openid")
    state = (os.environ.get("WX_SUBSCRIBE_MINIPROGRAM_STATE") or "formal").strip().lower()
    if state not in {"developer", "trial", "formal"}:
        raise SubscribeMessageError("小程序跳转环境配置无效", "bad_config")
    payload = {
        "touser": openid,
        "template_id": config["template_id"],
        "page": config["page"],
        "miniprogram_state": state,
        "lang": "zh_CN",
        "data": build_data(config, values),
    }
    token = wechat_vpay.access_token()
    result = _post_json(API_URL + "?" + urllib.parse.urlencode({"access_token": token}), payload)
    errcode = int(result.get("errcode") or 0)
    if errcode:
        raise SubscribeMessageError(
            result.get("errmsg") or "订阅消息发送失败",
            str(errcode),
            result,
        )
    return result
