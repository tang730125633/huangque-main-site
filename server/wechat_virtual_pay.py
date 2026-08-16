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
import base64
import json
import os
import secrets
import struct
import threading
import time
import urllib.parse
import urllib.request

try:
    from .content_domains import pricing
except ImportError:  # 生产环境以脚本方式从 /home/ubuntu/auth-service 启动
    from content_domains import pricing


API_BASE = "https://api.weixin.qq.com"
_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE = {"value": "", "expires_at": 0}
TOKEN_INVALID_CODES = {40001, 40014, 42001}

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

MEMBERSHIP_PRODUCT = {
    "id": "membership_experience",
    "product_id": "hq_member_exp_1y",
    "title": "一年体验官",
    "recommended": False,
    "order_type": "membership_experience",
}
MEMBERSHIP_RENEWAL_PRODUCT = {
    "id": "membership_experience_renewal",
    "product_id": "hq_exp_renew_1y",
    "title": "体验官续费一年",
    "recommended": False,
    "order_type": "membership_experience_renewal",
}

CUSTOM_MIN_AMOUNT_YUAN = 1
CUSTOM_MAX_AMOUNT_YUAN = 5000


class VirtualPayError(RuntimeError):
    def __init__(self, message, code="wechat_error", response=None):
        super().__init__(message)
        self.code = code
        self.response = response or {}


class MessagePushError(RuntimeError):
    pass


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _hmac_hex(key, message):
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def message_push_token():
    return (os.environ.get("WX_MESSAGE_PUSH_TOKEN") or "").strip()


def message_push_aes_key():
    value = (os.environ.get("WX_MESSAGE_PUSH_AES_KEY") or "").strip()
    if value and len(value) != 43:
        raise MessagePushError("WX_MESSAGE_PUSH_AES_KEY 必须是 43 位 EncodingAESKey")
    return value


def message_push_configured():
    try:
        return bool(message_push_token() and message_push_aes_key() and (os.environ.get("WX_MP_APPID") or "").strip())
    except Exception:
        return False


def message_signature(*parts):
    return hashlib.sha1("".join(sorted(str(part or "") for part in parts)).encode("utf-8")).hexdigest()


def _message_aes_key():
    value = message_push_aes_key()
    if not value:
        raise MessagePushError("消息推送 EncodingAESKey 未配置")
    try:
        key = base64.b64decode(value + "=")
    except Exception as exc:
        raise MessagePushError("消息推送 EncodingAESKey 无效") from exc
    if len(key) != 32:
        raise MessagePushError("消息推送 EncodingAESKey 无效")
    return key


def _pkcs7_pad(value):
    padding = 32 - (len(value) % 32)
    return value + bytes([padding]) * padding


def _pkcs7_unpad(value):
    if not value:
        raise MessagePushError("消息解密结果为空")
    padding = value[-1]
    if padding < 1 or padding > 32 or value[-padding:] != bytes([padding]) * padding:
        raise MessagePushError("消息填充无效")
    return value[:-padding]


def decrypt_message(ciphertext):
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        key = _message_aes_key()
        encrypted = base64.b64decode(str(ciphertext or ""))
        decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
        plain = _pkcs7_unpad(decryptor.update(encrypted) + decryptor.finalize())
        if len(plain) < 20:
            raise MessagePushError("消息解密长度无效")
        size = struct.unpack("!I", plain[16:20])[0]
        message = plain[20:20 + size]
        received_appid = plain[20 + size:].decode("utf-8")
        expected_appid = (os.environ.get("WX_MP_APPID") or "").strip()
        if not expected_appid or not secrets.compare_digest(received_appid, expected_appid):
            raise MessagePushError("消息 AppID 校验失败")
        return message.decode("utf-8")
    except MessagePushError:
        raise
    except Exception as exc:
        raise MessagePushError("消息解密失败") from exc


def encrypt_message(message):
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        key = _message_aes_key()
        appid = (os.environ.get("WX_MP_APPID") or "").strip().encode("utf-8")
        if not appid:
            raise MessagePushError("小程序 AppID 未配置")
        payload = str(message or "").encode("utf-8")
        plain = secrets.token_bytes(16) + struct.pack("!I", len(payload)) + payload + appid
        encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
        encrypted = encryptor.update(_pkcs7_pad(plain)) + encryptor.finalize()
        return base64.b64encode(encrypted).decode("ascii")
    except MessagePushError:
        raise
    except Exception as exc:
        raise MessagePushError("消息加密失败") from exc


def verify_message_url(query):
    token = message_push_token()
    if not token:
        raise MessagePushError("消息推送 Token 未配置")
    timestamp = (query.get("timestamp") or [""])[0]
    nonce = (query.get("nonce") or [""])[0]
    echo = (query.get("echostr") or [""])[0]
    msg_sig = (query.get("msg_signature") or [""])[0]
    if msg_sig:
        expected = message_signature(token, timestamp, nonce, echo)
        if not secrets.compare_digest(msg_sig, expected):
            raise MessagePushError("消息推送验签失败")
        return decrypt_message(echo)
    signature = (query.get("signature") or [""])[0]
    expected = message_signature(token, timestamp, nonce)
    if not signature or not secrets.compare_digest(signature, expected):
        raise MessagePushError("消息推送验签失败")
    return echo


def decode_message_push(query, body):
    token = message_push_token()
    if not token:
        raise MessagePushError("消息推送 Token 未配置")
    timestamp = (query.get("timestamp") or [""])[0]
    nonce = (query.get("nonce") or [""])[0]
    encrypted = body.get("Encrypt") or body.get("encrypt")
    if encrypted:
        provided = (query.get("msg_signature") or [""])[0]
        expected = message_signature(token, timestamp, nonce, encrypted)
        if not provided or not secrets.compare_digest(provided, expected):
            raise MessagePushError("消息推送验签失败")
        try:
            return json.loads(decrypt_message(encrypted)), True
        except json.JSONDecodeError as exc:
            raise MessagePushError("消息推送 JSON 无效") from exc
    signature = (query.get("signature") or [""])[0]
    expected = message_signature(token, timestamp, nonce)
    if not signature or not secrets.compare_digest(signature, expected):
        raise MessagePushError("消息推送验签失败")
    return body, False


def encode_message_push(payload, encrypted):
    if not encrypted:
        return payload
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(8)
    ciphertext = encrypt_message(compact_json(payload))
    return {
        "Encrypt": ciphertext,
        "MsgSignature": message_signature(message_push_token(), timestamp, nonce, ciphertext),
        "TimeStamp": timestamp,
        "Nonce": nonce,
    }


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
    values = [item for item in values if str(item.get("id") or "").strip() not in (MEMBERSHIP_PRODUCT["id"], MEMBERSHIP_RENEWAL_PRODUCT["id"])]
    membership_price_fen = pricing.get_price("membership.experience.price_yuan") * 100
    membership_points = pricing.get_price("membership.experience.bonus_points")
    values.append(dict(MEMBERSHIP_PRODUCT, price_fen=membership_price_fen, points=membership_points))
    values.append(dict(MEMBERSHIP_RENEWAL_PRODUCT, price_fen=membership_price_fen, points=0))
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
            "order_type": str(item.get("order_type") or "points").strip(),
        }
        if not product["id"] or product["id"] in seen:
            raise VirtualPayError("虚拟支付商品 id 缺失或重复", "bad_config")
        if not product["product_id"] or len(product["product_id"]) > 20:
            raise VirtualPayError("虚拟支付 product_id 无效", "bad_config")
        if product["price_fen"] <= 0 or product["points"] < 0 or not product["title"]:
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
        payload = compact_json({
            "grant_type": "client_credential",
            "appid": appid,
            "secret": secret,
            "force_refresh": False,
        })
        result = _json_request(API_BASE + "/cgi-bin/stable_token", payload)
        if result.get("errcode") or not result.get("access_token"):
            raise VirtualPayError(result.get("errmsg") or "微信 access_token 获取失败", "access_token_failed", result)
        _TOKEN_CACHE["value"] = result["access_token"]
        _TOKEN_CACHE["expires_at"] = now + int(result.get("expires_in") or 7200)
        return _TOKEN_CACHE["value"]


def invalidate_access_token(token=""):
    """Clear only the rejected cached token, preserving a newer concurrent value."""
    with _TOKEN_LOCK:
        if token and _TOKEN_CACHE["value"] != token:
            return False
        _TOKEN_CACHE["value"] = ""
        _TOKEN_CACHE["expires_at"] = 0
        return True


def payment_params(product, order_id, session_key, purchase=None):
    env = pay_env()
    purchase = purchase or purchase_for(product)
    quantity = int(purchase["quantity"])
    goods_price = int(product["price_fen"])
    selling_price = int(purchase["amount_fen"]) // quantity
    sign_obj = {
        "offerId": offer_id(),
        "buyQuantity": quantity,
        "env": env,
        "currencyType": "CNY",
        "productId": product["product_id"],
        "goodsPrice": goods_price,
        "outTradeNo": order_id,
        "attach": "points:" + str(purchase["points"]),
    }
    if selling_price < goods_price:
        sign_obj["activitySellingPrice"] = selling_price
    sign_data = compact_json(sign_obj)
    return {
        "mode": "short_series_goods",
        "signData": sign_data,
        "paySig": calc_pay_sig("requestVirtualPayment", sign_data, app_key(env)),
        "signature": calc_signature(sign_data, session_key),
    }


def _xpay(uri, payload, signed=True):
    post_body = compact_json(payload)

    def request_with_token(token):
        query = {"access_token": token}
        if signed:
            query["pay_sig"] = calc_pay_sig(uri, post_body, app_key(int(payload.get("env", pay_env()))))
        return _json_request(API_BASE + uri + "?" + urllib.parse.urlencode(query), post_body)

    token = access_token()
    result = request_with_token(token)
    if int(result.get("errcode") or 0) in TOKEN_INVALID_CODES:
        invalidate_access_token(token)
        result = request_with_token(access_token())
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
    # 现网接口会校验 pay_sig；与查询订单相同，签名内容必须覆盖完整请求体。
    return _xpay("/xpay/notify_provide_goods", {
        "order_id": order_id,
        "env": pay_env() if env is None else int(env),
    })
