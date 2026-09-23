"""Select only current-account, unexpired CLI uploads for the new templates."""
import hashlib
import json
import math
import random
import re
import time

from . import cli_uploads

INSET = "inset-flip-whip"
OPENING = "fixed-opening-whip"
BILINGUAL = "bilingual-stagger-salon"
IDS = (INSET, OPENING, BILINGUAL)
WINDOWS = {INSET: [5.] * 7, OPENING: [153/30, 85/30, 153/30, 78/30]}


def available(username):
    if not username:
        raise ValueError("请先登录并上传本人视频素材")
    owner = hashlib.sha256(str(username).encode("utf-8")).hexdigest()
    records, seen = [], set()
    root = cli_uploads.UPLOAD_ROOT.resolve()
    for index, path in enumerate(root.glob("vid_*.json")):
        if index >= 10000:
            raise ValueError("账号素材索引繁忙，请显式选择本人素材")
        try:
            if path.is_symlink() or path.stat().st_size > 4096:
                continue
            meta = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(meta, dict) or meta.get("owner_hash") != owner:
                continue
            duration = meta.get("duration")
            sha = str(meta.get("sha256") or "")
            extension = {"video/mp4": ".mp4", "video/quicktime": ".mov"}.get(meta.get("mime"))
            if (meta.get("version") != 1 or not extension
                    or not cli_uploads.VIDEO_UPLOAD_ID_RE.fullmatch(path.stem)
                    or not re.fullmatch(r"[0-9a-f]{64}", sha)
                    or sha in seen or float(meta.get("expires_at") or 0) <= time.time()
                    or type(duration) not in (int, float) or not math.isfinite(duration)
                    or duration < 2.1):
                continue
            data = root / (path.stem + extension)
            if data.is_symlink() or not data.is_file():
                continue
            seen.add(sha)
            records.append({"upload_id": path.stem, "media_type": "video", "duration": float(duration)})
        except (OSError, ValueError, TypeError):
            continue
    random.SystemRandom().shuffle(records)
    return records[:20]


def choose(records, windows, *, rng=None):
    """Longest windows first; no repeated source within one video."""
    rng = rng or random.SystemRandom()
    remaining = list(records)
    chosen = [None] * len(windows)
    for index in sorted(range(len(windows)), key=lambda i: windows[i], reverse=True):
        eligible = [r for r in remaining if r["duration"] >= windows[index] + .1]
        if not eligible:
            raise ValueError(f"本人视频素材不足，需要 {len(windows)} 段不重复且时长足够的视频，请先上传")
        item = rng.choice(eligible)
        remaining.remove(item)
        chosen[index] = {k: v for k, v in item.items() if k in {"upload_id", "sha256", "media_type"}}
        chosen[index]["clip_start_seconds"] = math.floor(rng.uniform(0, min(3600, item["duration"]-windows[index]-.1))*1000)/1000
    return chosen


def initial(template_id, username):
    records = available(username)
    if template_id != BILINGUAL:
        return choose(records, WINDOWS[template_id]), None
    if len(records) < 3:
        raise ValueError("双语字幕模板需要至少 3 段已上传的本人视频")
    return [{"upload_id": r["upload_id"], "media_type": "video"} for r in records], [r["duration"] for r in records]


def explicit_durations(materials, username):
    durations = []
    for item in materials:
        if item.get("media_type") != "video":
            raise ValueError("双语字幕模板只支持视频素材")
        handle, _, meta = cli_uploads.open_upload("video", item["upload_id"], username)
        handle.close()
        duration = meta.get("duration")
        if type(duration) not in (int, float) or not math.isfinite(duration):
            raise ValueError("本人视频时长无效")
        durations.append(float(duration))
    return durations


def bilingual(materials, durations, duration, *, explicit=False):
    if not isinstance(durations, list) or len(materials) != len(durations):
        raise ValueError("双语模板缺少冻结的本人素材时长")
    if explicit:
        count = len(materials)
        if not 3 <= count <= 20:
            raise ValueError("双语字幕模板需要 3-20 段本人视频")
        if len({item["sha256"] for item in materials}) != count:
            raise ValueError("同一条视频不可使用重复素材")
        for i, (item, length) in enumerate(zip(materials, durations)):
            window = max(2., math.ceil((duration/count + (.18 if i < count-1 else 0))*30-1e-6)/30)
            if length - item.get("clip_start_seconds", 0) < window + .1:
                raise ValueError("本人素材时长不足以覆盖配音，请上传更长的视频")
        return [dict(item) for item in materials]
    records, seen = [], set()
    for item, length in zip(materials, durations):
        sha = item.get("sha256")
        if sha in seen:
            continue
        seen.add(sha)
        records.append(dict(item, duration=length))
    maximum = min(20, len(records))
    preferred = min(maximum, max(3, math.ceil(duration/2.8)))
    counts = list(range(preferred, maximum+1)) + list(range(preferred-1, 2, -1))
    for count in counts:
        if count < 3:
            continue
        windows = [max(2., math.ceil((duration/count + (.18 if i < count-1 else 0))*30-1e-6)/30) for i in range(count)]
        try:
            return choose(records, windows)
        except ValueError:
            continue
    raise ValueError("本人素材总时长不足以覆盖配音，请上传更多或更长的视频")
