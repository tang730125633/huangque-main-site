# -*- coding: utf-8 -*-
"""一键成片：编导分镜脚本 → 按风格分路由 → 生成视频
  口播/种草 → 数字人口播 (gen_video)
  剧情     → 果肉视频   (gen_xiaole_video)"""
from .core import adb, closing


def gen_script_to_video(payload):
    """由 run_job 调用，走标准 job 生命周期。"""
    username = (payload.get("_username") or "").strip()
    scenes = payload.get("scenes") or []
    style = (payload.get("style") or "口播").strip()

    if style == "剧情":
        return _gen_drama(username, scenes, payload)
    return _gen_talking(username, scenes, payload)


def _gen_talking(username, scenes, payload):
    """口播/种草：拼接 line → 取形象 → 调 talking avatar"""
    lines = []
    for s in scenes:
        line = (s.get("line") or "").strip()
        if line:
            lines.append(line)
    if not lines:
        raise ValueError("脚本中没有口播文案，请先生成脚本")
    full_text = "\n\n".join(lines)

    avatar_id = payload.get("avatar_id")
    if avatar_id:
        from .video import get_video_avatar
        avatar = get_video_avatar(username, str(avatar_id))
    else:
        avatar = _get_first_avatar(username)
    if not avatar:
        raise ValueError(
            "你还没有创建数字人形象。请先在视频页上传一张人物照片创建形象，再回来一键成片。"
        )

    from .video import gen_video

    video_payload = {
        "_username": username,
        "_job_id": payload.get("_job_id"),
        "mode": "text",
        "text": full_text,
        "avatar_id": str(avatar["id"]),
        "voice": payload.get("voice") or "S_d21F8OR62",
        "resolution": payload.get("resolution") or "720p",
        "ratio": payload.get("ratio") or "9:16",
        "motion": payload.get("motion") or "medium",
        "subtitle": payload.get("subtitle", True),
        "subtitle_style": payload.get("subtitle_style") or "white",
        "subtitle_position": payload.get("subtitle_position") or "bottom",
    }
    result = gen_video(video_payload)
    result["type"] = "script_to_video"
    result["scene_count"] = len(scenes)
    result["pipeline"] = "talking"
    return result


def _gen_drama(username, scenes, payload):
    """剧情：拼接 scene 画面描述 → 果肉(Grok)文生视频"""
    descs = []
    for s in scenes:
        scene = (s.get("scene") or "").strip()
        if scene:
            descs.append(scene)
    if not descs:
        raise ValueError("脚本中没有画面描述，请先生成脚本")
    full_prompt = "、".join(descs) + "。连贯运镜，电影质感，竖屏"

    from .video import gen_xiaole_video

    grok_payload = {
        "_username": username,
        "_job_id": payload.get("_job_id"),
        "channel": "grok",
        "prompt": full_prompt,
        "ratio": payload.get("ratio") or "9:16",
        "duration": payload.get("duration") or 10,
        "resolution": payload.get("resolution") or "720p",
    }
    result = gen_xiaole_video(grok_payload)
    result["scene_count"] = len(scenes)
    result["pipeline"] = "grok"
    return result


def _get_first_avatar(username):
    """返回用户第一个可用形象，没有则返回 None。"""
    try:
        with closing(adb()) as c:
            row = c.execute(
                "SELECT id, name, image_file FROM avatars"
                " WHERE username=? AND status!='deleted'"
                " ORDER BY id ASC LIMIT 1",
                (username,),
            ).fetchone()
        if row:
            return dict(row)
    except Exception:
        pass
    return None


HANDLERS = {"script_to_video": gen_script_to_video}
