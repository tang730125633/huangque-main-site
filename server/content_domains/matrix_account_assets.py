"""Read-only IP12 account index -> verified COS bytes for template jobs.

Never import the Agent application or mutate its index/cache. COS is read using
the content service's existing credential, after account/index ownership checks.
"""
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil

ROOT = Path(os.environ.get("MATRIX_ACCOUNT_ASSET_ROOT", "/home/ubuntu/hq-ip-agent/data/assets"))
TYPES = {".mp4": ("video", "video/mp4"), ".mov": ("video", "video/quicktime"),
    ".jpg": ("image", "image/jpeg"), ".jpeg": ("image", "image/jpeg"),
    ".png": ("image", "image/png"), ".webp": ("image", "image/webp"),
    ".mp3": ("audio", "audio/mpeg"), ".m4a": ("audio", "audio/mp4"),
    ".wav": ("audio", "audio/wav")}
MAX_BYTES = 512 * 1024 * 1024


def records(account_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(account_id or "")):
        raise ValueError("无效账号素材身份")
    root = ROOT.resolve()
    account = root / account_id
    index = account / "index.json"
    if account.is_symlink() or index.is_symlink() or account.resolve().parent != root:
        raise ValueError("账号素材索引路径无效")
    if not index.is_file():
        return []
    if index.stat().st_size > 32*1024*1024:
        raise ValueError("账号素材索引过大")
    data = json.loads(index.read_text(encoding="utf-8"))
    items = data.get("items")
    if not isinstance(items, list):
        raise ValueError("账号素材索引无效")
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        aid, ext = str(item.get("id", "")), str(item.get("ext", "")).lower()
        if not re.fullmatch(r"[0-9a-f]{32}", aid) or ext not in TYPES or item.get("status") == "deleted":
            continue
        expected = f"hq-materials/{account_id}/{aid}{ext}"
        key = item.get("cos_key")
        # Cross-account or generated-main-site keys must never become raw inputs.
        if key and key != expected:
            continue
        result.append({"asset_id": aid, "media_type": TYPES[ext][0], "mime": TYPES[ext][1],
            "ext": ext, "cos_key": key or "", "sha256": item.get("sha256") or "",
            "size": item.get("size"), "source": item.get("source", ""),
            "account_id": account_id})
    return result


def select(account_id, materials):
    items = records(account_id)
    if materials["mode"] == "auto":
        # Raw uploads/derived clips, never our prior finished videos.
        visuals = [i for i in items if i["media_type"] in {"video", "image"}
            and i["source"] != "agent"]
        random.SystemRandom().shuffle(visuals)
        return visuals[:20]
    lookup = {item["asset_id"]: item for item in items}
    result = []
    for aid in materials["ids"]:
        if re.fullmatch(r"(?:vid|img)_[0-9a-f]{32}", aid):
            result.append({"upload_id": aid, "media_type": "video" if aid.startswith("vid_") else "image"})
            continue
        item = lookup.get(aid)
        if not item or item["media_type"] not in {"video", "image"}:
            raise ValueError("MATERIAL_UNAVAILABLE: 素材不属于当前账号或已删除")
        result.append(item)
    return result


def fetch(item, target):
    from . import cos
    if not re.fullmatch(r"[0-9a-f]{32}", item["asset_id"]):
        raise ValueError("素材身份无效")
    # Recheck ownership/deletion, but require the frozen identity to match.
    current = next((r for r in records(item["account_id"]) if r["asset_id"] == item["asset_id"]), None)
    if current is None or any(current[k] != item[k] for k in ("cos_key", "sha256", "ext")):
        raise ValueError("MATERIAL_UNAVAILABLE: 素材已删除或发生变化")
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if item["cos_key"]:
        head = cos.head(item["cos_key"])
        length = int(head.get("Content-Length", -1))
        if not 0 < length <= MAX_BYTES:
            raise ValueError("素材文件大小无效")
        cos.download(cos._object_key(item["cos_key"]), target)
    else:
        source = ROOT.resolve() / item["account_id"] / (item["asset_id"] + item["ext"])
        if source.is_symlink() or not source.is_file() or not 0 < source.stat().st_size <= MAX_BYTES:
            raise ValueError("MATERIAL_UNAVAILABLE: 原素材不存在")
        length = source.stat().st_size
        shutil.copyfile(source, target)
    if target.stat().st_size != length:
        raise RuntimeError("素材中转不完整")
    with target.open("rb") as handle:
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024*1024), b""):
            digest.update(chunk)
        sha = digest.hexdigest()
    if item["sha256"] and sha != item["sha256"]:
        raise RuntimeError("素材文件校验失败")
    return sha


def resolve(selected, username, directory):
    from . import matrix_template_video as matrix
    result, provenance = [], []
    for index, item in enumerate(selected):
        if item.get("upload_id"):
            resolved = matrix._resolve_user_materials([item], username)[0]
            identity = item["upload_id"]
        else:
            local = Path(directory) / (str(index) + item["ext"])
            sha = fetch(item, local)
            with local.open("rb") as stream:
                matrix._upload_user_asset(stream, local.stat().st_size, sha, item["mime"])
            resolved = {"sha256": sha, "media_type": item["media_type"]}
            identity = item["asset_id"]
        result.append(resolved)
        provenance.append({"asset_id": identity, "sha256": resolved["sha256"]})
    return result, provenance
