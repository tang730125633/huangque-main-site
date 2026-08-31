# -*- coding: utf-8 -*-
"""一键成片：现有数字人口播 + 用户图片资产/按需生图 + FFmpeg 自动穿插。"""
import json
import ipaddress
import os
import random
import re
import subprocess
import time
import urllib.parse
import urllib.error
import urllib.request

from .core import OUT_DIR, adb, closing, jdb

MAX_MATERIAL_SCENES = 8
PHOTO_MOTIONS = ("zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down")
MATERIAL_IMAGE_RETRY_CODES = {520}
MATERIAL_IMAGE_RETRY_DELAY = 2
DIGITAL_HUMAN_MATERIAL_UPLOAD_PATH = "/api/gen/script_to_video/material-upload"
DIGITAL_HUMAN_MATERIAL_UPLOAD_PURPOSE = "smart_montage"
DIGITAL_HUMAN_MATERIAL_UPLOAD_LEASE_SECONDS = 4 * 60 * 60
_UPLOAD_ID_RE = re.compile(r"^img_[0-9a-f]{32}$")


def _trusted_client_ip(handler):
    peer = (handler.client_address[0]
            if getattr(handler, "client_address", None) else "")
    forwarded = str(handler.headers.get("X-Real-IP") or "").strip()
    try:
        if ipaddress.ip_address(peer).is_loopback and forwarded:
            return str(ipaddress.ip_address(forwarded))
        return str(ipaddress.ip_address(peer))
    except ValueError:
        return "unknown"


def _scene_prompt(scene):
    return re.sub(r"\s+", " ", str((scene or {}).get("scene") or "")).strip()[:800]


def _bigrams(text):
    compact = re.sub(r"[\W_]+", "", (text or "").lower(), flags=re.UNICODE)
    return {compact[i:i + 2] for i in range(max(0, len(compact) - 1))}


def _similarity(left, right):
    a, b = _bigrams(left), _bigrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / float(max(1, min(len(a), len(b))))


def _result_candidates(result):
    if not isinstance(result, dict):
        return []
    candidates = []
    if result.get("file"):
        candidates.append((result.get("prompt") or "", result["file"]))
    for item in result.get("materials") or []:
        if isinstance(item, dict) and item.get("file"):
            candidates.append((item.get("prompt") or "", item["file"]))
    return candidates


def _safe_existing_image(rel):
    try:
        path = (OUT_DIR / str(rel)).resolve()
        path.relative_to(OUT_DIR.resolve())
        return path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    except Exception:
        return False


def _match_image_asset(username, prompt):
    """从本人最近图片/一键成片产物中找最接近的静态素材。"""
    with closing(jdb()) as conn:
        rows = conn.execute(
            "SELECT result FROM jobs WHERE username=? AND status='done'"
            " AND kind IN ('image','script_to_video') ORDER BY id DESC LIMIT 240",
            (username,),
        ).fetchall()
    best = None
    for row in rows:
        try:
            result = json.loads(row["result"] or "{}")
        except Exception:
            continue
        for old_prompt, rel in _result_candidates(result):
            score = _similarity(prompt, old_prompt)
            if score >= 0.34 and _safe_existing_image(rel) and (best is None or score > best[0]):
                best = (score, str(rel))
    return best[1] if best else None


def prepare_script_to_video_payload(
        payload, username, digital_human_consent=None):
    """提交扣点前冻结素材计划，保证能一次算清总价且不发生生成到一半欠费。"""
    body = dict(payload or {})
    from . import digital_human_oneclick, digital_human_v2
    pipeline = str(body.get("pipeline") or "").strip().lower()
    if pipeline == digital_human_oneclick.PIPELINE:
        return digital_human_oneclick.prepare_compose_payload(
            body, username, consent_record=digital_human_consent,
        )
    if pipeline == digital_human_v2.PIPELINE:
        return digital_human_v2.prepare_compose_payload(
            body, username, consent_record=digital_human_consent,
        )
    if str(body.get("pipeline") or "").strip() == "pixelle":
        from . import pixelle_video
        pixelle_video.require_available()
        return pixelle_video.prepare_payload(body, username)
    scenes = [dict(scene) for scene in (body.get("scenes") or []) if isinstance(scene, dict)]
    if not scenes:
        raise ValueError("没有可生成的分镜")
    if len(scenes) > MAX_MATERIAL_SCENES:
        raise ValueError("一键成片最多支持 %d 个分镜" % MAX_MATERIAL_SCENES)
    body["scenes"] = scenes
    if (body.get("style") or "口播").strip() == "剧情":
        return body

    plan = []
    for index, scene in enumerate(scenes):
        prompt = _scene_prompt(scene)
        if not prompt:
            continue
        existing = _match_image_asset(username, prompt)
        plan.append({
            "scene_index": index,
            "prompt": prompt,
            "source": "asset" if existing else "generate",
            "file": existing,
        })
    body["material_plan"] = plan
    body["material_generate_count"] = sum(1 for item in plan if item["source"] == "generate")
    return body


def gen_script_to_video(payload):
    """由 run_job 调用，走标准 job 生命周期。"""
    if payload.get("pipeline") == "pixelle":
        from . import pixelle_video
        return pixelle_video.generate(payload)
    username = (payload.get("_username") or "").strip()
    from . import digital_human_oneclick, digital_human_v2
    pipeline = str(payload.get("pipeline") or "").strip().lower()
    if pipeline == digital_human_oneclick.PIPELINE:
        return digital_human_oneclick.compose(payload)
    if pipeline == digital_human_v2.PIPELINE:
        return digital_human_v2.compose(payload)
    scenes = payload.get("scenes") or []
    style = (payload.get("style") or "口播").strip()
    if style == "剧情":
        return _gen_drama(username, scenes, payload)
    return _gen_talking(username, scenes, payload)


def dispatch_http(handler, method, verify_token, must_change_password):
    """Serve authenticated digital-human planning and recovery endpoints."""
    from . import digital_human_oneclick, digital_human_runs, digital_human_v2

    path = handler.path.split("?", 1)[0]
    run_match = re.fullmatch(
        r"/api/gen/digital-human-v2/runs/(dh-run-[A-Za-z0-9._:%-]{1,192})"
        r"(?:/(recover|abandon))?",
        path,
    )
    routes = {
        digital_human_oneclick.PLAN_PATH,
        digital_human_oneclick.CONSENT_PATH,
        digital_human_oneclick.GESTURE_RECOVERY_PATH,
        digital_human_oneclick.MATERIAL_RECOVERY_PATH,
        digital_human_oneclick.VIDEO_RECOVERY_PATH,
        digital_human_oneclick.HEYGEN_PREFLIGHT_PATH,
        digital_human_v2.PLAN_PATH,
        digital_human_v2.CONSENT_PATH,
        digital_human_v2.AUDIO_UPLOAD_PATH,
        digital_human_v2.MATERIAL_RESOLVE_PATH,
        digital_human_v2.HISTORY_PATH,
        digital_human_runs.CAPABILITY_PATH,
        digital_human_runs.QUOTE_PATH,
        digital_human_runs.RUNS_PATH,
        DIGITAL_HUMAN_MATERIAL_UPLOAD_PATH,
    }
    if path not in routes and not run_match:
        return False

    user = verify_token(handler._token())
    if not user:
        handler._send(401, {"detail": "未登录或登录已过期"})
        return True
    if must_change_password(user):
        handler._send(403, {"detail": "请先修改初始密码"})
        return True

    if path == DIGITAL_HUMAN_MATERIAL_UPLOAD_PATH:
        from . import cli_uploads, miniprogram_security

        if method == "DELETE":
            try:
                body = handler._json_body_strict()
                if (not isinstance(body, dict) or set(body) != {"upload_id"}
                        or not _UPLOAD_ID_RE.fullmatch(
                            str(body.get("upload_id") or "").strip().lower())):
                    raise ValueError("请求必须提供有效的 upload_id")
                cli_uploads.discard_image(body["upload_id"], user["username"])
                handler._send(200, {"ok": True})
            except ValueError as exc:
                handler._send(400, {
                    "detail": str(exc)[:220], "code": "invalid_image_discard",
                })
            return True
        if method != "POST":
            handler._method_not_allowed()
            return True

        uploaded = None
        try:
            if handler.headers.get("Transfer-Encoding"):
                raise ValueError("图片上传必须提供 Content-Length")
            length = int(handler.headers.get("Content-Length") or 0)
            content_type = (
                handler.headers.get("Content-Type") or ""
            ).split(";", 1)[0].strip().lower()
            uploaded = cli_uploads.store_image(
                handler.rfile, length, user["username"], content_type,
                handler.headers.get("X-HQ-Image-SHA256"),
            )
            data, meta = cli_uploads.read_image_bytes(
                uploaded["upload_id"], user["username"],
            )
            if miniprogram_security.configured():
                miniprogram_security.check_image(
                    data, "digital-human-material%s" % meta["extension"],
                    meta["mime"],
                )
            approved = cli_uploads.approve_image(
                uploaded["upload_id"], user["username"],
                DIGITAL_HUMAN_MATERIAL_UPLOAD_PURPOSE,
                lease_seconds=DIGITAL_HUMAN_MATERIAL_UPLOAD_LEASE_SECONDS,
            )
            handler._send(200, {
                **uploaded,
                "expires_at": int(approved.get("expires_at") or 0),
                "expires_in": max(
                    0, int(approved.get("expires_at") or 0) - int(time.time()),
                ),
                "width": int(approved.get("width") or 0),
                "height": int(approved.get("height") or 0),
            })
        except miniprogram_security.ContentRejected as exc:
            if uploaded:
                cli_uploads.discard_image(uploaded.get("upload_id"), user["username"])
            handler._send(400, {
                "detail": str(exc)[:220], "code": "content_rejected",
            })
        except miniprogram_security.SecurityUnavailable as exc:
            if uploaded:
                cli_uploads.discard_image(uploaded.get("upload_id"), user["username"])
            handler._send(503, {
                "detail": str(exc)[:220], "code": exc.code,
                "retry_after_ms": 5000,
            })
        except (TypeError, ValueError) as exc:
            if uploaded:
                cli_uploads.discard_image(uploaded.get("upload_id"), user["username"])
            handler._send(400, {
                "detail": str(exc)[:220], "code": "invalid_image_upload",
            })
        except OSError:
            if uploaded:
                cli_uploads.discard_image(uploaded.get("upload_id"), user["username"])
            handler._send(500, {
                "detail": "图片暂时无法保存", "code": "image_upload_failed",
            })
        return True

    if path == digital_human_runs.CAPABILITY_PATH:
        if method != "GET":
            handler._method_not_allowed()
            return True
        handler._send(200, digital_human_runs.capability_response())
        return True

    if path == digital_human_v2.HISTORY_PATH:
        if method != "GET":
            handler._method_not_allowed()
            return True
        try:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
            handler._send(200, digital_human_v2.history_response(
                user["username"],
                (query.get("limit") or [20])[0],
                (query.get("offset") or [0])[0],
            ))
        except digital_human_oneclick.DigitalHumanRequestError as exc:
            handler._send(exc.status, {
                "detail": str(exc)[:220], "code": exc.code,
            })
        except ValueError as exc:
            handler._send(400, {"detail": str(exc)[:220]})
        return True

    if run_match and method == "GET" and not run_match.group(2):
        try:
            run_id = urllib.parse.unquote(run_match.group(1))
            handler._send(200, digital_human_runs.status_response(
                run_id, user["username"],
            ))
        except digital_human_oneclick.DigitalHumanRequestError as exc:
            handler._send(exc.status, {
                "detail": str(exc)[:220], "code": exc.code,
            })
        return True

    if method != "POST":
        handler._method_not_allowed()
        return True

    try:
        def submit_run_job(kind, body, idempotency_key, expected_cost):
            endpoint = {
                "image": "/api/gen/image",
                "video": "/api/gen/video",
                "script_to_video": "/api/gen/script_to_video",
            }[kind]
            port = int(handler.server.server_address[1])
            raw = json.dumps(
                body, ensure_ascii=False, separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            headers = {
                "Authorization": "Bearer " + handler._token(),
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Idempotency-Key": idempotency_key,
                "X-HQ-Expected-Cost": str(int(expected_cost)),
                "X-HQ-Internal-Token": os.environ.get("HQ_INTERNAL_TOKEN", ""),
                "User-Agent": "huangque-digital-human-runner/1",
            }
            request = urllib.request.Request(
                "http://127.0.0.1:%d%s" % (port, endpoint),
                data=raw, headers=headers, method="POST",
            )
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
            )
            try:
                with opener.open(request, timeout=45) as response:
                    status = response.getcode()
                    result = response.read(2 * 1024 * 1024 + 1)
            except urllib.error.HTTPError as exc:
                status = exc.code
                result = exc.read(2 * 1024 * 1024 + 1)
            except (urllib.error.URLError, OSError) as exc:
                return 502, {
                    "detail": "子任务提交结果未知：" + str(exc)[:120],
                    "code": "child_submit_unknown",
                }
            if len(result) > 2 * 1024 * 1024:
                return 502, {"detail": "子任务响应过大", "code": "child_response_too_large"}
            try:
                payload = json.loads(result or b"{}")
            except Exception:
                payload = {"detail": "子任务返回格式无效", "code": "child_response_invalid"}
                status = 502
            return int(status), payload

        if path == digital_human_runs.QUOTE_PATH:
            response = digital_human_runs.quote_response(
                handler._json_body_strict(), user["username"],
            )
        elif path == digital_human_runs.RUNS_PATH:
            response = digital_human_runs.start_response(
                handler._json_body_strict(), user["username"],
                handler.headers.get("X-HQ-Expected-Cost"), submit_run_job,
            )
        elif run_match and run_match.group(2) == "recover":
            response = digital_human_runs.recover_response(
                urllib.parse.unquote(run_match.group(1)),
                handler._json_body_strict(), user["username"], submit_run_job,
            )
        elif run_match and run_match.group(2) == "abandon":
            response = digital_human_runs.abandon_response(
                urllib.parse.unquote(run_match.group(1)),
                handler._json_body_strict(), user["username"],
            )
        elif path == digital_human_oneclick.PLAN_PATH:
            response = digital_human_oneclick.plan_response(
                handler._json_body_strict(),
            )
        elif path == digital_human_v2.PLAN_PATH:
            response = digital_human_v2.plan_response(
                handler._json_body_strict(), user["username"],
            )
        elif path == digital_human_v2.AUDIO_UPLOAD_PATH:
            if handler.headers.get("Transfer-Encoding"):
                raise digital_human_oneclick.DigitalHumanRequestError(
                    "录音上传必须提供 Content-Length",
                    "audio_upload_length_required",
                )
            try:
                length = int(handler.headers.get("Content-Length") or 0)
            except (TypeError, ValueError) as exc:
                raise digital_human_oneclick.DigitalHumanRequestError(
                    "录音上传长度无效", "audio_upload_length_required",
                ) from exc
            response = digital_human_v2.audio_upload_response(
                handler.rfile, length, user["username"],
                handler.headers.get("X-HQ-Run-ID"),
                handler.headers.get("Content-Type"),
                handler.headers.get("X-HQ-Audio-SHA256"),
                client_ip=_trusted_client_ip(handler),
            )
        elif path == digital_human_v2.MATERIAL_RESOLVE_PATH:
            response = digital_human_v2.resolve_material_response(
                handler._json_body_strict(), user["username"],
            )
        elif path in {
                digital_human_oneclick.CONSENT_PATH,
                digital_human_v2.CONSENT_PATH}:
            body = handler._json_body_strict()
            if str(body.get("voice_mode") or "").strip().lower() == "existing":
                from . import audio as audio_domain
                audio_domain.resolve_audio_provider_voice(
                    user["username"], str(body.get("voice_ref") or "").strip(),
                )
            if path == digital_human_oneclick.CONSENT_PATH:
                response = digital_human_oneclick.consent_response(
                    body, user["username"], os.environ.get("HQ_INTERNAL_TOKEN", ""),
                )
            else:
                response = digital_human_v2.consent_response(
                    body, user["username"], os.environ.get("HQ_INTERNAL_TOKEN", ""),
                )
        elif path == digital_human_oneclick.GESTURE_RECOVERY_PATH:
            response = digital_human_oneclick.validate_gesture_recovery(
                handler._json_body_strict(), user["username"],
            )
        elif path == digital_human_oneclick.MATERIAL_RECOVERY_PATH:
            response = digital_human_oneclick.validate_material_recovery(
                handler._json_body_strict(), user["username"],
            )
        elif path == digital_human_oneclick.VIDEO_RECOVERY_PATH:
            response = digital_human_oneclick.validate_video_recovery(
                handler._json_body_strict(), user["username"],
            )
        else:
            from . import video as video_domain
            subtitle = video_domain.subtitle_runtime_preflight()
            response = dict(video_domain.heygen_upload_preflight())
            response["subtitle"] = subtitle
        handler._send(200, response)
    except digital_human_oneclick.DigitalHumanRequestError as exc:
        payload = {"detail": str(exc)[:220], "code": exc.code}
        if exc.invalid_job_ids:
            payload["invalid_job_ids"] = exc.invalid_job_ids
        if exc.status == 503:
            payload["retry_after_ms"] = 5000
        handler._send(exc.status, payload)
    except ValueError as exc:
        handler._send(400, {"detail": str(exc)[:220]})
    except Exception as exc:
        if path != digital_human_oneclick.HEYGEN_PREFLIGHT_PATH:
            raise
        handler._send(int(getattr(exc, "status", 503) or 503), {
            "detail": str(exc)[:220],
            "code": str(getattr(exc, "code", "heygen_upload_unavailable")),
            "no_charge": True,
        })
    return True


def _material_images(plan):
    from . import image as image_domain

    materials = []
    try:
        for item in plan:
            rel = item.get("file")
            source = item.get("source")
            if source == "generate":
                image_payload = {
                    "prompt": item["prompt"], "ratio": "9:16", "quality": "standard",
                    "provider": "openai", "count": 1,
                }
                try:
                    generated = image_domain.gen_image(image_payload)
                except Exception as exc:
                    # 520 来自出境中转的瞬时异常。此时整段数字人口播已经生成并计费；
                    # 只补偿重试当前图片一次，比重跑整条 HeyGen 成片的成本低得多。
                    # 已生成的前序图片保留在 materials 中，不重复调用。
                    if getattr(exc, "code", None) not in MATERIAL_IMAGE_RETRY_CODES:
                        raise
                    time.sleep(MATERIAL_IMAGE_RETRY_DELAY)
                    generated = image_domain.gen_image(image_payload)
                rel = generated.get("file")
            if not rel or not _safe_existing_image(rel):
                raise RuntimeError("分镜 %d 的素材不可用" % (int(item["scene_index"]) + 1))
            materials.append({
                "scene_index": int(item["scene_index"]),
                "prompt": item["prompt"],
                "source": source,
                "file": str(rel),
            })
        return materials
    except Exception:
        _cleanup_generated_materials(materials)
        raise


def _cleanup_generated_materials(materials):
    for item in materials:
        if item.get("source") != "generate":
            continue
        try:
            (OUT_DIR / item["file"]).resolve().unlink(missing_ok=True)
        except Exception:
            pass


def _scene_ranges(scenes, duration):
    weights = []
    for scene in scenes:
        line = str(scene.get("line") or "").strip()
        try:
            declared = float(str(scene.get("dur") or "").lower().replace("s", ""))
        except (TypeError, ValueError):
            declared = 0
        weights.append(declared if declared > 0 else max(1, len(line)))
    total = sum(weights) or len(scenes) or 1
    cursor, ranges = 0.0, []
    for weight in weights:
        span = duration * weight / total
        ranges.append((cursor, min(duration, cursor + span)))
        cursor += span
    return ranges


def _photo_motion_filter(width, height):
    """为静态素材随机选择轻微 Ken Burns 动效；只改变剪辑，不调用视频生成 API。"""
    motion = random.choice(PHOTO_MOTIONS)
    center = "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    if motion == "zoom_in":
        effect = "z='min(zoom+0.0008,1.08)':" + center
    elif motion == "zoom_out":
        effect = "z='if(eq(on,0),1.08,max(1.001,zoom-0.0008))':" + center
    else:
        progress = "min(on/200\\,1)"
        axes = {
            "pan_left":  ("(iw-iw/zoom)*%s" % progress, "(ih-ih/zoom)/2"),
            "pan_right": ("(iw-iw/zoom)*(1-%s)" % progress, "(ih-ih/zoom)/2"),
            "pan_up":    ("(iw-iw/zoom)/2", "(ih-ih/zoom)*%s" % progress),
            "pan_down":  ("(iw-iw/zoom)/2", "(ih-ih/zoom)*(1-%s)" % progress),
        }
        x, y = axes[motion]
        effect = "z=1.06:x='%s':y='%s'" % (x, y)
    return "zoompan=%s:d=1:s=%dx%d:fps=25" % (effect, width, height)


def _compose_materials(video_file, scenes, materials):
    if not materials:
        return video_file
    from . import video as video_domain

    source = video_domain._resolve_out_file(video_file)
    if not source:
        raise RuntimeError("数字人口播成片文件不存在")
    duration = video_domain._probe_video_duration(video_file)
    width, height = video_domain._probe_video_size(source)
    ranges = _scene_ranges(scenes, duration)
    command = ["ffmpeg", "-y", "-i", str(source)]
    for material in materials:
        command.extend(["-loop", "1", "-i", str((OUT_DIR / material["file"]).resolve())])

    filters, previous = [], "[0:v]"
    for pos, material in enumerate(materials):
        index = material["scene_index"]
        start, end = ranges[index]
        # 每个分镜中段穿插静态素材，前后保留数字人，避免整片只剩图片。
        show_start = start + (end - start) * 0.20
        show_end = start + (end - start) * 0.78
        prepared, output = "[mat%d]" % pos, "[mix%d]" % pos
        filters.append(
            "[%d:v]scale=%d:%d:force_original_aspect_ratio=increase,"
            "crop=%d:%d,setsar=1,%s%s" %
            (pos + 1, width, height, width, height,
             _photo_motion_filter(width, height), prepared)
        )
        filters.append(
            "%s%soverlay=0:0:enable='between(t,%.3f,%.3f)'%s" %
            (previous, prepared, show_start, show_end, output)
        )
        previous = output
    output = video_domain.VIDEO_OUT_DIR / ("script_broll_%d.mp4" % int(time.time() * 1000))
    command.extend([
        "-filter_complex", ";".join(filters), "-map", previous, "-map", "0:a?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-c:a", "copy",
        "-t", "%.3f" % duration, "-shortest", "-movflags", "+faststart", str(output),
    ])
    subprocess.run(command, check=True, timeout=900, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return video_domain._faststart_video_file(output.resolve().relative_to(OUT_DIR.resolve()).as_posix())


def _gen_talking(username, scenes, payload):
    """先生成完整数字人口播，再按分镜在中段穿插用户资产或新生成静态图。"""
    lines = [(scene.get("line") or "").strip() for scene in scenes]
    lines = [line for line in lines if line]
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
        raise ValueError("你还没有创建数字人形象。请先在视频页上传人物照片创建形象。")

    from . import video as video_domain

    want_subtitle = payload.get("subtitle", True)
    material_plan = payload.get("material_plan") or []
    result = video_domain.gen_video({
        "_username": username,
        "_job_id": payload.get("_job_id"),
        "mode": "text",
        "text": full_text,
        "avatar_id": str(avatar["id"]),
        "voice": payload.get("voice") or "S_d21F8OR62",
        "resolution": payload.get("resolution") or "720p",
        "ratio": payload.get("ratio") or "9:16",
        "motion": payload.get("motion") or "medium",
        "motion_prompt": payload.get("motion_prompt") or "",
        "subtitle": False if material_plan else want_subtitle,
    })
    materials = _material_images(material_plan)
    try:
        if materials:
            composed = _compose_materials(result.get("video_file"), scenes, materials)
            if want_subtitle:
                composed = video_domain.burn_subtitle(
                    composed, known_text=full_text,
                    style_key=payload.get("subtitle_style") or "white",
                    job_id=payload.get("_job_id"),
                    position=payload.get("subtitle_position") or "bottom",
                )
            result["plain_video_file"] = result.get("video_file")
            result["video_file"] = composed
            result["video_url"] = video_domain.public_url(composed, "video/mp4", private=True)
    except Exception:
        _cleanup_generated_materials(materials)
        raise
    result.update({
        "type": "script_to_video",
        "scene_count": len(scenes),
        "pipeline": "talking_with_materials" if material_plan else "talking",
        "materials": materials,
        "material_generated_count": sum(1 for item in materials if item["source"] == "generate"),
        "material_reused_count": sum(1 for item in materials if item["source"] == "asset"),
    })
    return result


def _gen_drama(username, scenes, payload):
    """剧情模式保持现有果肉视频链路。"""
    descs = [(scene.get("scene") or "").strip() for scene in scenes]
    descs = [desc for desc in descs if desc]
    if not descs:
        raise ValueError("脚本中没有画面描述，请先生成脚本")
    from .video import gen_xiaole_video
    result = gen_xiaole_video({
        "_username": username,
        "_job_id": payload.get("_job_id"),
        "channel": "grok",
        "prompt": "、".join(descs) + "。连贯运镜，电影质感，竖屏",
        "ratio": payload.get("ratio") or "9:16",
        "duration": payload.get("duration") or 10,
        "model": payload.get("model") or "grok-imagine-video",
        "resolution": payload.get("resolution") or "720p",
    })
    result.update({"type": "script_to_video", "scene_count": len(scenes), "pipeline": "grok"})
    return result


def _get_first_avatar(username):
    try:
        with closing(adb()) as conn:
            row = conn.execute(
                "SELECT id, name, image_file FROM avatars WHERE username=?"
                " AND status!='deleted' ORDER BY id ASC LIMIT 1",
                (username,),
            ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


HANDLERS = {"script_to_video": gen_script_to_video}
