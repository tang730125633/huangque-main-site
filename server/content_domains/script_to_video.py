# -*- coding: utf-8 -*-
"""一键成片：编导分镜脚本 → 自动拼接口播 → 取用户形象 → 生成数字人口播视频"""
from .core import adb, closing


def gen_script_to_video(payload):
    """由 run_job 调用，走标准 job 生命周期。"""
    username = (payload.get("_username") or "").strip()
    scenes = payload.get("scenes") or []

    # ① 拼接全部口播文案
    lines = []
    for s in scenes:
        line = (s.get("line") or "").strip()
        if line:
            lines.append(line)
    if not lines:
        raise ValueError("脚本中没有口播文案，请先生成脚本")
    full_text = "\n\n".join(lines)

    # ② 取用户第一个可用形象
    avatar = _get_first_avatar(username)
    if not avatar:
        raise ValueError(
            "你还没有创建数字人形象。请先在视频页上传一张人物照片创建形象，再回来一键成片。"
        )

    # ③ 调用现有视频管线
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
