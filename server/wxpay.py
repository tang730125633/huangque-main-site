"""微信支付 V3 客户端(公钥模式)。

延续本仓库零三方依赖习惯——仅用系统已装的 cryptography(49.x) 做 RSA 验签 +
AES-256-GCM 解密 notify，其余走标准库。商户凭证全部走环境变量,值不入库、不进 git。
配置项见 deploy/huangque-secrets.env.example 的 wxpay 段。

对外只暴露:
  configured()          -> bool          是否已配齐(未配时下单/回调路由回 503,不影响服务启动)
  create_native(...)    -> code_url       Native 扫码下单,返回二维码内容
  create_jsapi(...)     -> {prepay_id..}  小程序 JSAPI 下单(需 openid;登录流程未接前先备着)
  verify_notify(...)    -> bool           验 notify 签名(公钥模式)
  decrypt_resource(...) -> dict           解密 notify 的 resource

金额单位:对外 create_* 收「分」(int);调用方负责元→分换算并核对。
"""
import base64
import json
import os
import pathlib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidSignature

API_BASE = "https://api.mch.weixin.qq.com"
_NOTIFY_MAX_SKEW = 300  # notify 时间戳容差(秒),防重放


class WxPayNotConfigured(RuntimeError):
    pass


def _env(key, default=""):
    return (os.environ.get(key) or default).strip()


_cfg = None
_cfg_lock = threading.Lock()


def _config():
    """懒加载并缓存配置。缺项/密钥长度不对时抛 WxPayNotConfigured。"""
    global _cfg
    if _cfg is not None:
        return _cfg
    with _cfg_lock:
        if _cfg is not None:
            return _cfg
        fields = {
            "mchid": _env("WX_PAY_MCH_ID"),
            "serial": _env("WX_PAY_MCH_CERT_SERIAL_NO"),
            "key_path": _env("WX_PAY_PRIVATE_KEY_PATH"),
            "v3": _env("WX_PAY_API_V3_KEY"),
            "pub_path": _env("WX_PAY_PUBLIC_KEY_PATH"),
            "pub_id": _env("WX_PAY_PUBLIC_KEY_ID"),
            "appid": _env("WX_PAY_APPID"),
            "notify_url": _env("WX_PAY_NOTIFY_URL"),
        }
        missing = [k for k, v in fields.items() if not v]
        if missing:
            raise WxPayNotConfigured("缺少微信支付配置: " + ",".join(missing))
        if len(fields["v3"].encode()) != 32:
            raise WxPayNotConfigured("WX_PAY_API_V3_KEY 必须正好 32 字节")
        fields["priv"] = serialization.load_pem_private_key(
            pathlib.Path(fields["key_path"]).read_bytes(), password=None)
        fields["pub"] = serialization.load_pem_public_key(
            pathlib.Path(fields["pub_path"]).read_bytes())
        _cfg = fields
    return _cfg


def configured():
    try:
        _config()
        return True
    except Exception:
        return False


def _authorization(method, url_path, body):
    c = _config()
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex
    message = ("%s\n%s\n%s\n%s\n%s\n" % (method, url_path, ts, nonce, body)).encode()
    signature = c["priv"].sign(message, padding.PKCS1v15(), hashes.SHA256())
    return ('WECHATPAY2-SHA256-RSA2048 '
            'mchid="%s",nonce_str="%s",signature="%s",timestamp="%s",serial_no="%s"'
            % (c["mchid"], nonce, base64.b64encode(signature).decode(), ts, c["serial"]))


def _request(method, url_path, body_obj=None):
    body = json.dumps(body_obj, ensure_ascii=False) if body_obj is not None else ""
    headers = {
        "Authorization": _authorization(method, url_path, body),
        "Accept": "application/json",
        "User-Agent": "huangque-wxpay/1.0",
    }
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        API_BASE + url_path, data=body.encode() if body else None,
        method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def create_native(out_trade_no, description, total_fen):
    """Native 扫码下单,返回 code_url(二维码内容)。total_fen 为整数分。"""
    c = _config()
    status, data = _request("POST", "/v3/pay/transactions/native", {
        "appid": c["appid"],
        "mchid": c["mchid"],
        "description": description,
        "out_trade_no": out_trade_no,
        "notify_url": c["notify_url"],
        "amount": {"total": int(total_fen), "currency": "CNY"},
    })
    if status == 200 and data.get("code_url"):
        return data["code_url"]
    raise RuntimeError("Native 下单失败: HTTP %s %s"
                       % (status, json.dumps(data, ensure_ascii=False)[:300]))


def create_jsapi(out_trade_no, description, total_fen, openid):
    """小程序 JSAPI 下单,返回 prepay_id。需先有用户 openid。

    ponytail: 客户端唤起支付要的 paySign 二次签名(和这里的请求签名是两套东西)在
    调用方按 openid 拿到 prepay_id 后再算;此函数只负责拿 prepay_id。openid 流程
    (wx.login code2session)尚未接入 auth_server,接入后此函数即可用。
    """
    c = _config()
    status, data = _request("POST", "/v3/pay/transactions/jsapi", {
        "appid": c["appid"],
        "mchid": c["mchid"],
        "description": description,
        "out_trade_no": out_trade_no,
        "notify_url": c["notify_url"],
        "amount": {"total": int(total_fen), "currency": "CNY"},
        "payer": {"openid": openid},
    })
    if status == 200 and data.get("prepay_id"):
        return data["prepay_id"]
    raise RuntimeError("JSAPI 下单失败: HTTP %s %s"
                       % (status, json.dumps(data, ensure_ascii=False)[:300]))


def verify_notify(sig_headers, body_bytes):
    """验 notify 签名(公钥模式)。sig_headers 支持 .get() 取 Wechatpay-* 头。

    校验:①时间戳在 5 分钟内(防重放) ②序列号=我方公钥ID ③公钥验签通过。
    """
    c = _config()
    ts = (sig_headers.get("Wechatpay-Timestamp") or "").strip()
    nonce = (sig_headers.get("Wechatpay-Nonce") or "").strip()
    sig = (sig_headers.get("Wechatpay-Signature") or "").strip()
    serial = (sig_headers.get("Wechatpay-Serial") or "").strip()
    if not (ts and nonce and sig):
        return False
    try:
        if abs(int(time.time()) - int(ts)) > _NOTIFY_MAX_SKEW:
            return False
    except ValueError:
        return False
    # 公钥模式:微信下发的 Serial 应等于我方公钥 ID
    if serial and serial != c["pub_id"]:
        return False
    message = ("%s\n%s\n%s\n" % (ts, nonce, body_bytes.decode())).encode()
    try:
        c["pub"].verify(base64.b64decode(sig), message, padding.PKCS1v15(), hashes.SHA256())
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def decrypt_resource(resource):
    """解密 notify 的 resource(AEAD_AES_256_GCM),返回明文 dict。"""
    c = _config()
    aesgcm = AESGCM(c["v3"].encode())
    plain = aesgcm.decrypt(
        resource["nonce"].encode(),
        base64.b64decode(resource["ciphertext"]),
        (resource.get("associated_data") or "").encode(),
    )
    return json.loads(plain.decode())


# ==================== 小程序 JSAPI 专用(openid + 调起支付签名) ====================

def jscode2session(js_code):
    """wx.login 的 code 换 openid。用小程序 AppID/AppSecret(WX_MP_*),与商户凭证是两套。

    返回 openid;失败抛 RuntimeError(带微信 errcode/errmsg)。
    """
    appid = _env("WX_MP_APPID") or _env("WX_PAY_APPID")
    secret = _env("WX_MP_APPSECRET")
    if not (appid and secret):
        raise WxPayNotConfigured("缺少 WX_MP_APPID / WX_MP_APPSECRET")
    url = ("https://api.weixin.qq.com/sns/jscode2session?appid=%s&secret=%s&js_code=%s"
           "&grant_type=authorization_code" % (appid, secret, urllib.parse.quote(js_code)))
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.loads(r.read().decode() or "{}")
    if data.get("openid"):
        return data["openid"]
    raise RuntimeError("jscode2session 失败: %s" % json.dumps(data, ensure_ascii=False)[:200])


def jsapi_pay_params(prepay_id):
    """把 prepay_id 打包成小程序 wx.requestPayment 所需的参数(含 paySign)。

    paySign 是「调起支付」的二次签名:对 appId\\ntimeStamp\\nnonceStr\\npackage\\n 用商户私钥签。
    与请求签名(_authorization)是两处不同的签名,别混。
    """
    c = _config()
    appid = _env("WX_MP_APPID") or c["appid"]
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex
    package = "prepay_id=%s" % prepay_id
    message = ("%s\n%s\n%s\n%s\n" % (appid, ts, nonce, package)).encode()
    sign = base64.b64encode(c["priv"].sign(message, padding.PKCS1v15(), hashes.SHA256())).decode()
    return {"appId": appid, "timeStamp": ts, "nonceStr": nonce,
            "package": package, "signType": "RSA", "paySign": sign}
