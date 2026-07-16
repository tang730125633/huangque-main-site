#!/usr/bin/env python3
"""将服务端配置的虚拟商品上传并发布到微信对应环境。"""
import json
import os
import sys
import time

try:
    from . import wechat_virtual_pay as vpay
except ImportError:
    import wechat_virtual_pay as vpay


ITEM_URL = os.environ.get(
    "WX_VIRTUAL_PAY_ITEM_URL",
    "https://huangquechuanmei.com/assets/cloud/logo-bird.png",
).strip()


def wait_for(uri, key, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = vpay._xpay(uri, {"env": vpay.pay_env()})
        status = int(result.get("status") or 0)
        print(json.dumps({"stage": key, "status": status, key: result.get(key) or []}, ensure_ascii=False))
        if status == 3:
            return result
        if status == 2:
            raise RuntimeError("%s 失败或部分失败" % key)
        time.sleep(3)
    raise TimeoutError("等待 %s 超时" % key)


def main():
    if not vpay.is_configured():
        raise RuntimeError("请先配置 offerId、对应环境 AppKey、AppID 和 AppSecret")
    products = vpay.products()
    upload_items = [
        {
            "id": item["product_id"],
            "name": item["title"][:20],
            "price": item["price_fen"],
            "remark": "黄雀 AI 生成任务点数，购买后自动到账",
            "item_url": ITEM_URL,
        }
        for item in products
    ]
    vpay._xpay("/xpay/start_upload_goods", {"upload_item": upload_items, "env": vpay.pay_env()})
    wait_for("/xpay/query_upload_goods", "upload_item")
    vpay._xpay("/xpay/start_publish_goods", {
        "publish_item": [{"id": item["product_id"]} for item in products],
        "env": vpay.pay_env(),
    })
    wait_for("/xpay/query_publish_goods", "publish_item")
    print("虚拟商品已发布到%s环境" % ("现网" if vpay.pay_env() == 0 else "沙箱"))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ERROR:", exc, file=sys.stderr)
        raise SystemExit(1)
