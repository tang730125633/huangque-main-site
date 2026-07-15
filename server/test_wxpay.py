#!/usr/bin/env python3
"""wxpay 自检:验签、AES-GCM 解密、下单请求签名格式。全离线,不碰网络/真实凭证。

跑法: python3 server/test_wxpay.py   (需系统已装 cryptography)
"""
import base64
import json
import os
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import wxpay


def _fake_config():
    """伪造一套配置塞进 wxpay 的缓存,避免依赖真实凭证/环境变量。"""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    v3 = "0123456789abcdef0123456789abcdef"  # 正好 32 字节
    wxpay._cfg = {
        "mchid": "1111592799", "serial": "ABC", "v3": v3,
        "pub_id": "PUB_KEY_ID_TEST", "appid": "wxtest", "notify_url": "https://x/notify",
        "priv": priv, "pub": priv.public_key(),   # 用同一对密钥模拟微信平台签名
    }
    return wxpay._cfg


def test_verify_notify_roundtrip():
    c = _fake_config()
    ts, nonce = str(int(time.time())), "nonce123"
    body = b'{"resource":{}}'
    msg = ("%s\n%s\n%s\n" % (ts, nonce, body.decode())).encode()
    sig = base64.b64encode(c["priv"].sign(msg, padding.PKCS1v15(), hashes.SHA256())).decode()
    headers = {"Wechatpay-Timestamp": ts, "Wechatpay-Nonce": nonce,
               "Wechatpay-Signature": sig, "Wechatpay-Serial": "PUB_KEY_ID_TEST"}
    assert wxpay.verify_notify(headers, body) is True, "正确签名应通过"

    # 篡改 body -> 验签必败
    assert wxpay.verify_notify(headers, b'{"resource":{},"evil":1}') is False, "篡改应被拒"
    # 过期时间戳 -> 防重放
    old = dict(headers); old["Wechatpay-Timestamp"] = str(int(time.time()) - 9999)
    assert wxpay.verify_notify(old, body) is False, "过期时间戳应被拒"
    # 序列号不符 -> 拒
    bad = dict(headers); bad["Wechatpay-Serial"] = "PUB_KEY_ID_WRONG"
    assert wxpay.verify_notify(bad, body) is False, "序列号不符应被拒"


def test_decrypt_resource_roundtrip():
    c = _fake_config()
    payload = {"out_trade_no": "R123ABC", "transaction_id": "42000", "trade_state": "SUCCESS",
               "amount": {"total": 3900}}
    aesgcm = AESGCM(c["v3"].encode())
    nonce_str = "abcDEF123456"   # 真实 notify 的 nonce 是 12 位 ASCII 随机串
    aad = b"transaction"
    ct = aesgcm.encrypt(nonce_str.encode(), json.dumps(payload).encode(), aad)
    resource = {"algorithm": "AEAD_AES_256_GCM", "nonce": nonce_str,
                "ciphertext": base64.b64encode(ct).decode(), "associated_data": aad.decode()}
    got = wxpay.decrypt_resource(resource)
    assert got == payload, "解密结果应与原文一致"


def test_authorization_format():
    _fake_config()
    auth = wxpay._authorization("POST", "/v3/pay/transactions/native", '{"a":1}')
    assert auth.startswith("WECHATPAY2-SHA256-RSA2048 "), "签名头前缀不对"
    for field in ("mchid=", "nonce_str=", "signature=", "timestamp=", "serial_no="):
        assert field in auth, "签名头缺字段 " + field


def test_jsapi_pay_params():
    c = _fake_config()
    params = wxpay.jsapi_pay_params("wx20211111prepayid")
    for k in ("appId", "timeStamp", "nonceStr", "package", "signType", "paySign"):
        assert k in params, "调起支付参数缺 " + k
    assert params["package"] == "prepay_id=wx20211111prepayid"
    assert params["signType"] == "RSA"
    # paySign 应能被同一对公钥验回(证明签名对的是 appId\ntimeStamp\nnonceStr\npackage\n)
    msg = ("%s\n%s\n%s\n%s\n" % (params["appId"], params["timeStamp"],
                                 params["nonceStr"], params["package"])).encode()
    c["pub"].verify(base64.b64decode(params["paySign"]), msg, padding.PKCS1v15(), hashes.SHA256())


def test_package_whitelist():
    # 与 auth_server.RECHARGE_PACKAGES 对齐:金额只认固定组合
    import auth_server
    assert auth_server.RECHARGE_PACKAGES.get(100) == 39.0
    assert auth_server.RECHARGE_PACKAGES.get(2400) == 598.0
    assert auth_server.RECHARGE_PACKAGES.get(999999) is None, "非法套餐必须返回 None(拒绝下单)"


if __name__ == "__main__":
    test_verify_notify_roundtrip()
    test_decrypt_resource_roundtrip()
    test_authorization_format()
    test_jsapi_pay_params()
    test_package_whitelist()
    print("wxpay 自检全部通过 ✅")
