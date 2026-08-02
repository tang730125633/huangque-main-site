#!/usr/bin/env python3
"""Wan2.2 图生动作隔离 POC；默认只预览请求和费用。"""
import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request


MODEL = "wan2.2-animate-move"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com"
PRICE_CNY_PER_SECOND = {"wan-std": 0.4, "wan-pro": 0.6}


def _https_url(value, label):
    parsed = urllib.parse.urlsplit(str(value))
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{label}必须是公网 HTTPS URL")
    return value


def build_payload(identity_image_url, motion_video_url, mode="wan-std", watermark=False):
    if mode not in PRICE_CNY_PER_SECOND:
        raise ValueError("mode 必须是 wan-std 或 wan-pro")
    return {
        "model": MODEL,
        "input": {
            "image_url": _https_url(identity_image_url, "人物图片"),
            "video_url": _https_url(motion_video_url, "动作视频"),
            "watermark": bool(watermark),
        },
        "parameters": {"check_image": True, "mode": mode},
    }


def estimate_cost(seconds, mode):
    if not 2 <= seconds <= 30:
        raise ValueError("参考视频时长必须在 2 到 30 秒之间")
    return round(seconds * PRICE_CNY_PER_SECOND[mode], 2)


def _request_json(method, url, api_key, payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    })
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:1000]
        raise RuntimeError(f"Wan 请求失败（HTTP {exc.code}）：{detail}") from exc


def submit(payload, api_key, base_url=DEFAULT_BASE_URL):
    return _request_json(
        "POST",
        base_url.rstrip("/") + "/api/v1/services/aigc/image2video/video-synthesis",
        api_key,
        payload,
    )


def wait_for_result(task_id, api_key, base_url=DEFAULT_BASE_URL, timeout=40 * 60):
    deadline = time.time() + timeout
    url = base_url.rstrip("/") + "/api/v1/tasks/" + urllib.parse.quote(task_id)
    while time.time() < deadline:
        result = _request_json("GET", url, api_key)
        status = result.get("output", {}).get("task_status")
        if status in {"SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"}:
            return result
        time.sleep(15)
    raise TimeoutError("Wan 动作任务等待超时")


def _preview(payload):
    safe = json.loads(json.dumps(payload))
    for key in ("image_url", "video_url"):
        parsed = urllib.parse.urlsplit(safe["input"][key])
        safe["input"][key] = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return safe


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity-image-url", required=True, help="已获授权的人物图片 HTTPS URL")
    parser.add_argument("--motion-video-url", required=True, help="2–30 秒动作视频 HTTPS URL")
    parser.add_argument("--mode", choices=PRICE_CNY_PER_SECOND, default="wan-std")
    parser.add_argument("--expected-seconds", type=float, default=5, help="仅用于调用前估价")
    parser.add_argument("--watermark", action="store_true")
    parser.add_argument("--submit", action="store_true", help="提交真实付费任务")
    parser.add_argument("--confirm-authorized-person", action="store_true", help="确认人物素材已取得授权")
    parser.add_argument("--wait", action="store_true", help="提交后等待终态")
    args = parser.parse_args(argv)

    try:
        payload = build_payload(
            args.identity_image_url, args.motion_video_url, args.mode, args.watermark
        )
        cost = estimate_cost(args.expected_seconds, args.mode)
    except ValueError as exc:
        parser.error(str(exc))

    print(json.dumps({"estimated_cost_cny": cost, "request": _preview(payload)}, ensure_ascii=False, indent=2))
    if not args.submit:
        return 0
    if os.environ.get("WAN_MOTION_POC_ALLOW_PAID") != "1":
        parser.error("付费提交前必须设置 WAN_MOTION_POC_ALLOW_PAID=1")
    if not args.confirm_authorized_person:
        parser.error("付费提交前必须传 --confirm-authorized-person")
    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        parser.error("未配置 DASHSCOPE_API_KEY")

    result = submit(payload, api_key, os.environ.get("DASHSCOPE_BASE_URL", DEFAULT_BASE_URL))
    task_id = result.get("output", {}).get("task_id")
    if args.wait and task_id:
        result = wait_for_result(
            task_id, api_key, os.environ.get("DASHSCOPE_BASE_URL", DEFAULT_BASE_URL)
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
