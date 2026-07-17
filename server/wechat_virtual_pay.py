#!/usr/bin/env python3
"""微信小程序虚拟支付（普通虚拟商品）客户端。

只使用 Python 标准库。所有敏感配置均来自服务端环境变量，绝不返回给小程序：

- WX_MP_APPID / WX_MP_APPSECRET
- WX_VIRTUAL_PAY_OFFER_ID
- WX_VIRTUAL_PAY_APP_KEY_PROD / WX_VIRTUAL_PAY_APP_KEY_SANDBOX
- WX_VIRTUAL_PAY_ENV（0 现网，1 沙箱）
- WX_VIRTUAL_PAY_PRODUCTS_JSON（可选，覆盖默认商品包）
"""
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.parse
import urllib.request


API_BASE = "https://api.weixin.qq.com"
_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE = {"value": "", "expires_at": 0}

DEFAULT_PRODUCTS = (
    {
        "id": "points_1000",
        "product_id": "hq_points_1000",
        "title": "1000 点",
        "price_fen": 9900,
        "points": 1000,
        "recommended": False,
    },
    {
        "id": "points_2000",
        "product_id": "hq_points_2000",
        "title": "2000 点",
        "price_fen": 19900,
        "points": 2000,
        "recommended": False,
    },
    {
        "id": "points_5000",
        "product_id": "hq_points_5000",
        "title": "5000 点",
        "price_fen": 49900,
        "points": 5000,
        "recommended": True,
    },
    {
        "id": "custom_points",
        "product_id": "hq_points_custom",
        "title": "自定义点数",
        "price_fen": 100,
        "points": 10,
        "recommended": False,
        "custom_amount": True,
    },
)

CUSTOM_MIN_AMOUNT_YUAN = 1
CUSTOM_MAX_AMOUNT_YUAN = 5000


class VirtualPayError(RuntimeError):
    def __init__(self, message, code="wechat_error", response=None):
        super().__init__(message)
        self.code = code
        self.response = response or {}


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _hmac_hex(key, message):
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def calc_pay_sig(uri, post_body, app_key):
    return _hmac_hex(app_key, uri + "&" + post_body)


def calc_signature(sign_data, session_key):
    return _hmac_hex(session_key, sign_data)


def pay_env():
    value = int(os.environ.get("WX_VIRTUAL_PAY_ENV", "0"))
    if value not in (0, 1):
        raise VirtualPayError("WX_VIRTUAL_PAY_ENV 只能是 0 或 1", "bad_config")
    return value


def app_key(env=None):
    env = pay_env() if env is None else int(env)
    name = "WX_VIRTUAL_PAY_APP_KEY_PROD" if env == 0 else "WX_VIRTUAL_PAY_APP_KEY_SANDBOX"
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise VirtualPayError("虚拟支付 AppKey 未配置", "not_configured")
    return value


def offer_id():
    value = (os.environ.get("WX_VIRTUAL_PAY_OFFER_ID") or "").strip()
    if not value:
        raise VirtualPayError("虚拟支付 offerId 未配置", "not_configured")
    return value


def products():
    raw = (os.environ.get("WX_VIRTUAL_PAY_PRODUCTS_JSON") or "").strip()
    values = json.loads(raw) if raw else list(DEFAULT_PRODUCTS)
    result = []
    seen = set()
    for item in values:
        product = {
            "id": str(item.get("id") or "").strip(),
            "product_id": str(item.get("product_id") or "").strip(),
            "title": str(item.get("title") or "").strip(),
            "price_fen": int(item.get("price_fen") or 0),
            "points": int(item.get("points") or 0),
            "recommended": bool(item.get("recommended")),
            "custom_amount": bool(item.get("custom_amount")),
        }
        if not product["id"] or product["id"] in seen:
            raise VirtualPayError("虚拟支付商品 id 缺失或重复", "bad_config")
        if not product["product_id"] or len(product["product_id"]) > 20:
            raise VirtualPayError("虚拟支付 product_id 无效", "bad_config")
        if product["price_fen"] <= 0 or product["points"] <= 0 or not product["title"]:
            raise VirtualPayError("虚拟支付商品价格、点数或名称无效", "bad_config")
        if product["custom_amount"] and product["price_fen"] != 100:
            raise VirtualPayError("虚拟支付自定义金额商品单价必须为1元", "bad_config")
        seen.add(product["id"])
        result.append(product)
    if sum(1 for item in result if item["custom_amount"]) > 1:
        raise VirtualPayError("虚拟支付自定义金额商品只能配置一个", "bad_config")
    return result


def product_by_id(package_id):
    for item in products():
        if item["id"] == package_id:
            return item
    return None


def custom_product():
    for item in products():
        if item["custom_amount"]:
            return item
    return None


def custom_quantity(value):
    """把用户输入转换为整数元数量；拒绝浮点数、布尔值和越界值。"""
    if isinstance(value, bool):
        return None
    text = str(value if value is not None else "").strip()
    if not text.isdigit():
        return None
    quantity = int(text)
    if quantity < CUSTOM_MIN_AMOUNT_YUAN or quantity > CUSTOM_MAX_AMOUNT_YUAN:
        return None
    return quantity


def purchase_for(product, custom_amount_yuan=None):
    """返回可信的购买数量、订单总额和到账点数。"""
    if product.get("custom_amount"):
        quantity = custom_quantity(custom_amount_yuan)
        if quantity is None:
            raise VirtualPayError(
                "自定义充值金额须为%d~%d元整数" % (
                    CUSTOM_MIN_AMOUNT_YUAN,
                    CUSTOM_MAX_AMOUNT_YUAN,
                ),
                "invalid_custom_amount",
            )
    else:
        quantity = 1
    return {
        "quantity": quantity,
        "amount_fen": int(product["price_fen"]) * quantity,
        "points": int(product["points"]) * quantity,
    }


def is_configured():
    try:
        if not (os.environ.get("WX_MP_APPID") or "").strip():
            return False
        if not (os.environ.get("WX_MP_APPSECRET") or "").strip():
            return False
        offer_id()
        app_key()
        products()
        return True
    except Exception:
        return False


def _json_request(url, body=None, timeout=15):
    data = None if body is None else body.encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="GET" if data is None else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8").strip()
            return json.loads(raw) if raw else {}
    except VirtualPayError:
        raise
    except Exception as exc:
        raise VirtualPayError("微信接口暂时不可用", "network_error") from exc


def code_to_session(code):
    code = (code or "").strip()
    appid = (os.environ.get("WX_MP_APPID") or "").strip()
    secret = (os.environ.get("WX_MP_APPSECRET") or "").strip()
    if not code:
        raise VirtualPayError("缺少微信登录 code", "bad_request")
    if not appid or not secret:
        raise VirtualPayError("小程序 AppID/AppSecret 未配置", "not_configured")
    query = urllib.parse.urlencode({
        "appid": appid,
        "secret": secret,
        "js_code": code,
        "grant_type": "authorization_code",
    })
    result = _json_request(API_BASE + "/sns/jscode2session?" + query)
    if result.get("errcode"):
        raise VirtualPayError(result.get("errmsg") or "微信登录态获取失败", "code2session_failed", result)
    if not result.get("openid") or not result.get("session_key"):
        raise VirtualPayError("微信登录态响应不完整", "code2session_failed", result)
    return result


def access_token():
    now = int(time.time())
    with _TOKEN_LOCK:
        if _TOKEN_CACHE["value"] and _TOKEN_CACHE["expires_at"] > now + 60:
            return _TOKEN_CACHE["value"]
        appid = (os.environ.get("WX_MP_APPID") or "").strip()
        secret = (os.environ.get("WX_MP_APPSECRET") or "").strip()
        if not appid or not secret:
            raise VirtualPayError("小程序 AppID/AppSecret 未配置", "not_configured")
        query = urllib.parse.urlencode({
            "grant_type": "client_credential",
            "appid": appid,
            "secret": secret,
        })
        result = _json_request(API_BASE + "/cgi-bin/token?" + query)
        if result.get("errcode") or not result.get("access_token"):
            raise VirtualPayError(result.get("errmsg") or "微信 access_token 获取失败", "access_token_failed", result)
        _TOKEN_CACHE["value"] = result["access_token"]
        _TOKEN_CACHE["expires_at"] = now + int(result.get("expires_in") or 7200)
        return _TOKEN_CACHE["value"]


def payment_params(product, order_id, session_key, purchase=None):
    env = pay_env()
    purchase = purchase or purchase_for(product)
    sign_obj = {
        "offerId": offer_id(),
        "buyQuantity": int(purchase["quantity"]),
        "env": env,
        "currencyType": "CNY",
        "productId": product["product_id"],
        "goodsPrice": int(product["price_fen"]),
        "outTradeNo": order_id,
        "attach": "points:" + str(purchase["points"]),
    }
    sign_data = compact_json(sign_obj)
    return {
        "mode": "short_series_goods",
        "signData": sign_data,
        "paySig": calc_pay_sig("requestVirtualPayment", sign_data, app_key(env)),
        "signature": calc_signature(sign_data, session_key),
    }


def _xpay(uri, payload, signed=True):
    post_body = compact_json(payload)
    query = {"access_token": access_token()}
    if signed:
        query["pay_sig"] = calc_pay_sig(uri, post_body, app_key(int(payload.get("env", pay_env()))))
    result = _json_request(API_BASE + uri + "?" + urllib.parse.urlencode(query), post_body)
    if result.get("errcode"):
        raise VirtualPayError(result.get("errmsg") or "微信虚拟支付接口失败", "xpay_failed", result)
    return result


def query_order(openid, order_id, env=None):
    return _xpay("/xpay/query_order", {
        "openid": openid,
        "env": pay_env() if env is None else int(env),
        "order_id": order_id,
    })


def notify_provide_goods(order_id, env=None):
    # 该接口官方文档仅要求 access_token，不要求 pay_sig。
    return _xpay("/xpay/notify_provide_goods", {
        "order_id": order_id,
        "env": pay_env() if env is None else int(env),
    }, signed=False)
