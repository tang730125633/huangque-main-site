#!/usr/bin/env python3
"""Wan2.2 图生动作隔离 POC；默认只预览请求和费用。"""
import argparse
import json
import mimetypes
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


MODEL = "wan2.2-animate-move"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com"
PRICE_CNY_PER_SECOND = {"wan-std": 0.4, "wan-pro": 0.6}


def _media_url(value, label):
    parsed = urllib.parse.urlsplit(str(value))
    if parsed.scheme not in {"https", "oss"} or not parsed.netloc:
        raise ValueError(f"{label}必须是公网 HTTPS URL 或百炼临时 oss:// URL")
    return value


def build_payload(identity_image_url, motion_video_url, mode="wan-std", watermark=False):
    if mode not in PRICE_CNY_PER_SECOND:
        raise ValueError("mode 必须是 wan-std 或 wan-pro")
    return {
        "model": MODEL,
        "input": {
            "image_url": _media_url(identity_image_url, "人物图片"),
            "video_url": _media_url(motion_video_url, "动作视频"),
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
        "X-DashScope-OssResourceResolve": "enable",
    })
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:1000]
        raise RuntimeError(f"Wan 请求失败（HTTP {exc.code}）：{detail}") from exc


def _multipart_body(fields, file_path, boundary):
    chunks = []
    for name, value in fields:
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            str(value).encode(), b"\r\n",
        ])
    path = Path(file_path)
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    chunks.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        path.read_bytes(), b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    return b"".join(chunks)


def upload_temp_file(file_path, api_key, base_url=DEFAULT_BASE_URL):
    path = Path(file_path)
    if not path.is_file():
        raise ValueError(f"素材不存在：{path}")
    policy_url = base_url.rstrip("/") + "/api/v1/uploads?" + urllib.parse.urlencode({
        "action": "getPolicy", "model": MODEL,
    })
    policy = _request_json("GET", policy_url, api_key).get("data", {})
    required = (
        "policy", "signature", "upload_dir", "upload_host",
        "oss_access_key_id", "x_oss_object_acl", "x_oss_forbid_overwrite",
    )
    if any(not policy.get(key) for key in required):
        raise RuntimeError("百炼临时上传凭证不完整")
    max_bytes = int(policy.get("max_file_size_mb") or 0) * 1024 * 1024
    if max_bytes and path.stat().st_size > max_bytes:
        raise ValueError("素材超过百炼临时上传大小限制")

    object_key = policy["upload_dir"].rstrip("/") + "/" + secrets.token_hex(8) + path.suffix.lower()
    fields = [
        ("OSSAccessKeyId", policy["oss_access_key_id"]),
        ("Signature", policy["signature"]),
        ("policy", policy["policy"]),
        ("x-oss-object-acl", policy["x_oss_object_acl"]),
        ("x-oss-forbid-overwrite", policy["x_oss_forbid_overwrite"]),
        ("key", object_key),
        ("success_action_status", "200"),
    ]
    boundary = "----wan-motion-" + secrets.token_hex(12)
    request = urllib.request.Request(
        policy["upload_host"],
        data=_multipart_body(fields, path, boundary),
        method="POST",
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            if response.status != 200:
                raise RuntimeError(f"百炼临时素材上传失败（HTTP {response.status}）")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"百炼临时素材上传失败（HTTP {exc.code}）") from exc
    return "oss://" + object_key


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
