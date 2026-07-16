#!/usr/bin/env python3
"""将服务端配置的虚拟商品上传并发布到微信对应环境。"""
import json
import os
import re
import sys
import time

try:
    from . import wechat_virtual_pay as vpay
except ImportError:
    import wechat_virtual_pay as vpay


ITEM_URL = os.environ.get(
    "WX_VIRTUAL_PAY_ITEM_URL",
    "https://huangquechuanmei.com/assets/cloud/virtual-pay-item-200.png",
).strip()


def goods_name(value):
    """微信道具名称仅允许中英文、数字及 -_*·，不允许空格。"""
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff\-_*\u00b7]", "", str(value or ""))
    return cleaned[:20]


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


def submit_one_by_one(start_uri, query_uri, key, items):
    """微信虚拟支付商品接口每次最多提交 1 个道具。"""
    for item in items:
        vpay._xpay(start_uri, {key: [item], "env": vpay.pay_env()})
        wait_for(query_uri, key)


def main():
    if not vpay.is_configured():
        raise RuntimeError("请先配置 offerId、对应环境 AppKey、AppID 和 AppSecret")
    products = vpay.products()
    upload_items = [
        {
            "id": item["product_id"],
            "name": goods_name(item["title"]) or item["product_id"][:20],
            "price": item["price_fen"],
            "remark": "黄雀 AI 生成任务点数，购买后自动到账",
            "item_url": ITEM_URL,
        }
        for item in products
    ]
    submit_one_by_one(
        "/xpay/start_upload_goods",
        "/xpay/query_upload_goods",
        "upload_item",
        upload_items,
    )
    submit_one_by_one(
        "/xpay/start_publish_goods",
        "/xpay/query_publish_goods",
        "publish_item",
        [{"id": item["product_id"]} for item in products],
    )
    print("虚拟商品已发布到%s环境" % ("现网" if vpay.pay_env() == 0 else "沙箱"))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ERROR:", exc, file=sys.stderr)
        raise SystemExit(1)
