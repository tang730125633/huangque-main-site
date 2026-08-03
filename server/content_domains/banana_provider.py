# -*- coding: utf-8 -*-
"""Shared Nano Banana payload validation and Gemini image generation."""

import base64
import io
import json
import os
import time
import uuid

try:
    from PIL import Image
except Exception:  # pragma: no cover - production can run without post-crop support
    Image = None

MODELS = {"nb2": "gemini-3.1-flash-image", "pro": "gemini-3-pro-image"}
BASE_COST = {"nb2": {"std": 18, "hd": 35}, "pro": {"std": 35, "hd": 44}}
IMAGE_SIZES = {"nb2": {"std": "1K", "hd": "2K"}, "pro": {"std": "2K", "hd": "4K"}}
RATIOS = {"1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_REFERENCE_IMAGES = 5
MAX_TOTAL_REFERENCE_BYTES = 30 * 1024 * 1024


def clean_b64(value):
    raw = str(value or "").strip()
    mime = "image/png"
    if raw.startswith("data:") and "," in raw:
        meta, raw = raw.split(",", 1)
        mime = meta[5:].split(";", 1)[0].strip().lower() or mime
    return "".join(raw.split()), mime


def _validated_reference(value, index):
    if isinstance(value, str):
        data, mime = clean_b64(value)
    elif isinstance(value, dict):
        data, detected_mime = clean_b64(value.get("data"))
        mime = str(value.get("mime_type") or detected_mime or "image/png").strip().lower()
    else:
        raise ValueError("reference image %d is invalid" % (index + 1))
    if mime not in {"image/png", "image/jpeg", "image/webp"}:
        raise ValueError("reference image %d has an unsupported format" % (index + 1))
    try:
        decoded = base64.b64decode(data, validate=True)
    except Exception:
        raise ValueError("reference image %d must be valid base64" % (index + 1))
    if not decoded:
        raise ValueError("reference image %d is empty" % (index + 1))
    if len(decoded) > MAX_IMAGE_BYTES:
        raise ValueError("each reference image must be no larger than 10MB")
    return {"data": data, "mime_type": mime, "bytes": len(decoded)}


def validate_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("image request must be a JSON object")
    body = dict(payload)
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    if len(prompt) > 12000:
        raise ValueError("prompt must be no longer than 12000 characters")
    model = str(body.get("model") or "nb2").strip().lower()
    if model not in MODELS:
        raise ValueError("model only supports nb2/pro")
    quality = str(body.get("quality") or "std").strip().lower()
    if quality not in {"std", "hd"}:
        raise ValueError("quality only supports std/hd")
    ratio = str(body.get("ratio") or "1:1").strip()
    if ratio not in RATIOS:
        raise ValueError("unsupported image ratio")
    try:
        count = int(body.get("count") or 1)
    except (TypeError, ValueError):
        raise ValueError("count must be 1, 2, or 4")
    if count not in {1, 2, 4}:
        raise ValueError("count must be 1, 2, or 4")

    references = body.get("images")
    if references is None:
        references = [body["image"]] if body.get("image") else []
    if not isinstance(references, list) or len(references) > MAX_REFERENCE_IMAGES:
        raise ValueError("at most 5 reference images are supported")
    clean_references = [_validated_reference(value, index) for index, value in enumerate(references)]
    if sum(item["bytes"] for item in clean_references) > MAX_TOTAL_REFERENCE_BYTES:
        raise ValueError("reference images are too large in total")

    body.update({
        "prompt": prompt, "model": model, "quality": quality,
        "ratio": ratio, "count": count,
        "images": [
            {"data": item["data"], "mime_type": item["mime_type"]}
            for item in clean_references
        ],
    })
    body.pop("image", None)
    return body


def build_request_body(prompt, ratio, images=None, image_size=None):
    parts = [
        {"inlineData": {"mimeType": item["mime_type"], "data": item["data"]}}
        for item in (images or [])
    ]
    parts.append({"text": prompt})
    image_config = {"aspectRatio": ratio}
    if image_size:
        image_config["imageSize"] = image_size
    return {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": image_config,
        },
    }


def _parse_ratio(ratio):
    width, height = (int(value) for value in ratio.split(":", 1))
    return width / height


def _normalize_ratio(raw, ratio):
    if Image is None:
        return raw, None
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        image = image.convert("RGBA" if image.mode in {"RGBA", "LA"} else "RGB")
        source_ratio = image.width / image.height
        target_ratio = _parse_ratio(ratio)
        if abs(source_ratio - target_ratio) > 0.001:
            if source_ratio > target_ratio:
                width = max(1, int(image.height * target_ratio))
                left = max(0, (image.width - width) // 2)
                image = image.crop((left, 0, left + width, image.height))
            else:
                height = max(1, int(image.width / target_ratio))
                top = max(0, (image.height - height) // 2)
                image = image.crop((0, top, image.width, top + height))
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue(), {"width": image.width, "height": image.height}


def generate(payload, out_dir, public_url):
    body = validate_payload(payload)
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not configured")
    official_base = os.environ.get(
        "GEMINI_OFFICIAL_BASE", "https://generativelanguage.googleapis.com"
    ).rstrip("/")
    fallback_base = os.environ.get(
        "GEMINI_BASE", "https://generativelanguage.googleapis.com"
    ).rstrip("/")
    model_id = MODELS[body["model"]]
    image_size = IMAGE_SIZES[body["model"]][body["quality"]]
    request_body = build_request_body(
        body["prompt"], body["ratio"], body["images"], image_size
    )
    request_data = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
    from . import egress

    files, urls, dimensions = [], [], []
    for _index in range(body["count"]):
        response = egress.post_json(
            official_base,
            fallback_base,
            "/v1beta/models/%s:generateContent" % model_id,
            request_data,
            {"Content-Type": "application/json", "x-goog-api-key": api_key},
            log=lambda message: print(message, flush=True),
        )
        parts = (response.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        inline = next((part.get("inlineData") for part in parts if part.get("inlineData")), None)
        if not inline:
            detail = (response.get("error") or {}).get("message") or response
            raise ValueError("Nano Banana did not return an image: %s" % str(detail)[:180])
        raw, dimension = _normalize_ratio(base64.b64decode(inline["data"]), body["ratio"])
        filename = "nb_%d_%s.png" % (int(time.time() * 1000), uuid.uuid4().hex[:10])
        (out_dir / filename).write_bytes(raw)
        files.append(filename)
        urls.append(public_url(filename, "image/png"))
        if dimension:
            dimensions.append(dimension)
    result = {
        "type": "image",
        "mode": "nanobanana_multi_ref" if body["images"] else "nanobanana",
        "provider": "banana",
        "model": model_id,
        "model_key": body["model"],
        "image_size": image_size,
        "quality": body["quality"],
        "count": len(files),
        "file": files[0],
        "url": urls[0],
        "files": files,
        "urls": urls,
        "ratio": body["ratio"],
        "prompt": body["prompt"],
        "reference_count": len(body["images"]),
    }
    if dimensions:
        result.update({
            "width": dimensions[0]["width"],
            "height": dimensions[0]["height"],
            "dimensions": dimensions,
        })
    return result
