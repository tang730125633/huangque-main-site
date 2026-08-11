# -*- coding: utf-8 -*-
"""一键成片：现有数字人口播 + 用户图片资产/按需生图 + FFmpeg 自动穿插。"""
import json
import os
import pathlib
import random
import re
import subprocess
import shutil
import tempfile
import threading
import time
import uuid

from .core import OUT_DIR, adb, closing, jdb

MAX_MATERIAL_SCENES = 8
PHOTO_MOTIONS = ("zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down")
MATERIAL_IMAGE_RETRY_CODES = {520}
MATERIAL_IMAGE_RETRY_DELAY = 2
_MATERIAL_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
_MATERIAL_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"RIFF")
_material_job_locks = {}
_material_job_locks_guard = threading.Lock()


class ScriptToVideoRecoveryRequired(RuntimeError):
    """The paid provider may have accepted the job; retry from persisted state."""


class ScriptToVideoRecoveryStateUnavailable(RuntimeError):
    """Durable provider state cannot be read, so terminal handling must stop."""




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
        return _readable_image_path(path)
    except Exception:
        return False


def _readable_image_path(path):
    path = pathlib.Path(path)
    if not path.is_file() or path.suffix.lower() not in _MATERIAL_EXTENSIONS:
        return False
    try:
        if path.stat().st_size <= 0:
            return False
        with path.open("rb") as stream:
            head = stream.read(16)
        if head.startswith(_MATERIAL_MAGIC[:2]):
            return True
        return head.startswith(b"RIFF") and head[8:12] == b"WEBP"
    except OSError:
        return False


def _material_job_lock(job_id):
    key = int(job_id)
    with _material_job_locks_guard:
        return _material_job_locks.setdefault(key, threading.RLock())


def _load_job_payload(job_id, username=None):
    with closing(jdb()) as conn:
        row = conn.execute(
            "SELECT kind,username,status,payload FROM jobs WHERE id=?", (int(job_id),)
        ).fetchone()
    if not row or row["kind"] != "script_to_video":
        raise RuntimeError("文案成片任务不存在")
    if username is not None and row["username"] != username:
        raise PermissionError("素材不属于当前用户")
    try:
        payload = json.loads(row["payload"] or "{}")
    except Exception as exc:
        raise RuntimeError("文案成片任务数据损坏") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("文案成片任务数据损坏")
    return payload, str(row["status"] or "")


def _state_from_payload(payload):
    state = payload.get("_script_to_video_state") or {}
    return dict(state) if isinstance(state, dict) else {}


def _persist_job_state(job_id, username, phase, **fields):
    """Atomically persist the server-owned recovery state and heartbeat."""
    now = int(time.time())
    with closing(jdb()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT kind,username,status,payload FROM jobs WHERE id=?", (int(job_id),)
        ).fetchone()
        if not row or row["kind"] != "script_to_video" or row["username"] != username:
            conn.rollback()
            raise RuntimeError("文案成片任务状态不匹配")
        if row["status"] not in {"pending", "running"}:
            conn.rollback()
            raise RuntimeError("文案成片任务已结束")
        payload = json.loads(row["payload"] or "{}")
        if not isinstance(payload, dict):
            conn.rollback()
            raise RuntimeError("文案成片任务数据损坏")
        state = _state_from_payload(payload)
        state.update(fields)
        state.update({"version": 1, "phase": str(phase), "updated_at": now})
        payload["_script_to_video_state"] = state
        cur = conn.execute(
            "UPDATE jobs SET payload=?,updated_at=? WHERE id=? AND status IN ('pending','running')",
            (json.dumps(payload, ensure_ascii=False), now, int(job_id)),
        )
        if cur.rowcount != 1:
            conn.rollback()
            raise RuntimeError("文案成片任务状态保存失败")
        conn.commit()
    try:
        from . import video as video_domain
        video_domain.update_video_asset_phase(job_id, phase, strict=False)
    except Exception:
        pass
    return state


def _owned_source_asset(username, rel):
    rel = str(rel or "").strip()
    if not rel or not _safe_existing_image(rel):
        return False
    with closing(jdb()) as conn:
        rows = conn.execute(
            "SELECT result FROM jobs WHERE username=? AND status='done' "
            "AND kind IN ('image','script_to_video') ORDER BY id DESC LIMIT 500",
            (username,),
        ).fetchall()
    for row in rows:
        try:
            result = json.loads(row["result"] or "{}")
        except Exception:
            continue
        if any(str(candidate) == rel for _, candidate in _result_candidates(result)):
            return True
    return False


def _new_material_root(job_id):
    return "script_materials/%s-%s" % (int(job_id), uuid.uuid4().hex)


def _frozen_material_path(job_id, root, rel):
    expected = (OUT_DIR / str(root)).resolve()
    path = (OUT_DIR / str(rel)).resolve()
    expected.relative_to(OUT_DIR.resolve())
    path.relative_to(expected)
    if not pathlib.PurePosixPath(str(rel).replace("\\", "/")).parts[0] == "script_materials":
        raise ValueError("冻结素材路径无效")
    if not pathlib.Path(root).name.startswith("%s-" % int(job_id)):
        raise ValueError("冻结素材任务绑定无效")
    return path


def _copy_frozen_material(job_id, root, item, source_path):
    source_path = pathlib.Path(source_path).resolve()
    if not _readable_image_path(source_path):
        raise RuntimeError("分镜 %d 的素材为空、不可读或格式无效" % (int(item["scene_index"]) + 1))
    target_dir = (OUT_DIR / root).resolve()
    target_dir.relative_to(OUT_DIR.resolve())
    target_dir.mkdir(parents=True, exist_ok=True)
    suffix = source_path.suffix.lower()
    rel = "%s/scene-%02d%s" % (root, int(item["scene_index"]) + 1, suffix)
    target = _frozen_material_path(job_id, root, rel)
    temp = target.with_name(
        ".%s.%s%s" % (target.stem, uuid.uuid4().hex, suffix)
    )
    try:
        shutil.copyfile(str(source_path), str(temp))
        if not _readable_image_path(temp):
            raise RuntimeError("冻结素材复制后校验失败")
        os.replace(str(temp), str(target))
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
    return rel


def _frozen_materials_valid(job_id, state, plan):
    root = str(state.get("material_root") or "")
    items = state.get("materials") or []
    if not root or not isinstance(items, list) or len(items) != len(plan):
        return False
    expected = {int(item["scene_index"]) for item in plan}
    actual = set()
    try:
        for item in items:
            index = int(item["scene_index"])
            path = _frozen_material_path(job_id, root, item.get("file"))
            if not _readable_image_path(path):
                return False
            actual.add(index)
    except Exception:
        return False
    return actual == expected


def freeze_reused_materials_for_job(job_id, username):
    """Freeze historical assets before the request is enqueued.

    Generated images remain a worker pre-provider stage, but an old/history path
    is never left as the worker's only copy after this function succeeds.
    """
    with _material_job_lock(job_id):
        payload, _ = _load_job_payload(job_id, username)
        plan = payload.get("material_plan") or []
        state = _state_from_payload(payload)
        root = str(state.get("material_root") or "") or _new_material_root(job_id)
        frozen = {
            int(item["scene_index"]): dict(item)
            for item in (state.get("materials") or []) if isinstance(item, dict)
        }
        for item in plan:
            if item.get("source") != "asset":
                continue
            index = int(item["scene_index"])
            current = frozen.get(index)
            if current:
                path = _frozen_material_path(job_id, root, current.get("file"))
                if _readable_image_path(path):
                    continue
            rel = str(item.get("file") or "")
            if not _owned_source_asset(username, rel):
                raise PermissionError("分镜 %d 的历史素材不存在或不属于当前用户" % (index + 1))
            frozen[index] = dict(item, file=_copy_frozen_material(
                job_id, root, item, (OUT_DIR / rel).resolve()))
        ordered = [frozen[index] for index in sorted(frozen)]
        return _persist_job_state(
            job_id, username, "preparing_materials",
            material_root=root, materials=ordered,
        )


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


def prepare_script_to_video_payload(payload, username):
    """提交扣点前冻结素材计划，保证能一次算清总价且不发生生成到一半欠费。"""
    body = dict(payload or {})
    # Runtime recovery state is server-owned. Never accept a client-supplied
    # frozen path, provider id, or lifecycle phase.
    body.pop("_script_to_video_state", None)
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
    scenes = payload.get("scenes") or []
    style = (payload.get("style") or "口播").strip()
    if style == "剧情":
        return _gen_drama(username, scenes, payload)
    return _gen_talking(username, scenes, payload)


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


def _cleanup_material_root(job_id, state):
    root = str((state or {}).get("material_root") or "")
    if not root:
        return
    try:
        path = _frozen_material_path(job_id, root, root + "/placeholder").parent
        shutil.rmtree(str(path))
    except Exception:
        pass


def cleanup_unsubmitted_materials(job_id):
    state = get_recovery_state(job_id)
    if str(state.get("phase") or "") in {"preparing_materials", "materials_ready"}:
        _cleanup_material_root(job_id, state)


def _prepare_frozen_materials(job_id, username, plan):
    """Finish every material before the paid video create request."""
    if not job_id:
        raise RuntimeError("文案成片素材冻结缺少任务编号")
    with _material_job_lock(job_id):
        payload, _ = _load_job_payload(job_id, username)
        state = _state_from_payload(payload)
        if _frozen_materials_valid(job_id, state, plan):
            if state.get("phase") != "materials_ready":
                state = _persist_job_state(
                    job_id, username, "materials_ready",
                    material_root=state["material_root"], materials=state["materials"],
                )
            return [dict(item) for item in state["materials"]]

        root = str(state.get("material_root") or "") or _new_material_root(job_id)
        frozen = {}
        for item in (state.get("materials") or []):
            if not isinstance(item, dict):
                continue
            try:
                path = _frozen_material_path(job_id, root, item.get("file"))
                if _readable_image_path(path):
                    frozen[int(item["scene_index"])] = dict(item)
            except Exception:
                continue
        _persist_job_state(
            job_id, username, "preparing_materials",
            material_root=root, materials=[frozen[k] for k in sorted(frozen)],
        )
        generated_originals = []
        try:
            from . import image as image_domain
            for item in plan:
                index = int(item["scene_index"])
                if index in frozen:
                    continue
                source = str(item.get("source") or "")
                if source == "asset":
                    rel = str(item.get("file") or "")
                    if not _owned_source_asset(username, rel):
                        raise PermissionError(
                            "分镜 %d 的历史素材不存在或不属于当前用户" % (index + 1))
                elif source == "generate":
                    image_payload = {
                        "prompt": item["prompt"], "ratio": "9:16",
                        "quality": "standard", "provider": "openai", "count": 1,
                    }
                    try:
                        generated = image_domain.gen_image(image_payload)
                    except Exception as exc:
                        if getattr(exc, "code", None) not in MATERIAL_IMAGE_RETRY_CODES:
                            raise
                        time.sleep(MATERIAL_IMAGE_RETRY_DELAY)
                        generated = image_domain.gen_image(image_payload)
                    rel = str(generated.get("file") or "")
                    generated_originals.append(rel)
                else:
                    raise ValueError("分镜 %d 的素材来源无效" % (index + 1))
                source_path = (OUT_DIR / rel).resolve()
                source_path.relative_to(OUT_DIR.resolve())
                frozen[index] = dict(
                    item,
                    file=_copy_frozen_material(job_id, root, item, source_path),
                )
                _persist_job_state(
                    job_id, username, "preparing_materials", material_root=root,
                    materials=[frozen[k] for k in sorted(frozen)],
                )
            ordered = [frozen[int(item["scene_index"])] for item in plan]
            state = _persist_job_state(
                job_id, username, "materials_ready",
                material_root=root, materials=ordered,
            )
            return [dict(item) for item in state["materials"]]
        except Exception:
            _cleanup_material_root(job_id, {"material_root": root})
            raise
        finally:
            for rel in generated_originals:
                try:
                    source = (OUT_DIR / rel).resolve()
                    source.relative_to(OUT_DIR.resolve())
                    source.unlink(missing_ok=True)
                except Exception:
                    pass


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


def get_recovery_state(job_id):
    try:
        payload, _ = _load_job_payload(job_id)
        return _state_from_payload(payload)
    except Exception as exc:
        raise ScriptToVideoRecoveryStateUnavailable(
            "文案成片恢复状态暂不可读"
        ) from exc


def _provider_base_result(state):
    result = state.get("provider_result") or {}
    return dict(result) if isinstance(result, dict) else {}


def _provider_file_exists(result):
    try:
        from . import video as video_domain
        return bool(video_domain._resolve_out_file(result.get("video_file")))
    except Exception:
        return False


def recover_paid_job_error(job_id, error, requeue):
    """Keep a possibly billed script job recoverable instead of refunding it."""
    from . import video as video_domain

    if isinstance(error, video_domain.HeyGenProviderFailed):
        # The provider has explicitly reached a failed terminal state.  Unlike
        # an unknown POST outcome or an idempotent GET/download failure, there
        # is nothing left to resume, so the normal terminal CAS may refund once.
        return False
    state = get_recovery_state(job_id)
    phase = str(state.get("phase") or "")
    provider_id = str(state.get("provider_video_id") or "").strip()
    if phase == "provider_submitting" and not provider_id:
        # The create response may have been lost.  Re-POSTing would risk a
        # second charge, while refunding may give away a completed provider job.
        return True
    if phase in {"provider_submitted", "provider_completed", "composing", "done"}:
        if provider_id or _provider_file_exists(_provider_base_result(state)):
            requeue(job_id)
            return True
    return isinstance(error, ScriptToVideoRecoveryRequired)


def reclaim_orphaned_jobs(requeue, logger=print):
    """Requeue durable script jobs and hold ambiguous create requests."""
    try:
        with closing(jdb()) as conn:
            rows = conn.execute(
                "SELECT id,payload FROM jobs WHERE kind='script_to_video' AND status='running'"
            ).fetchall()
    except Exception as exc:
        raise ScriptToVideoRecoveryStateUnavailable(
            "文案成片启动恢复状态暂不可读"
        ) from exc
    handled = 0
    held = set()
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except Exception:
            payload = {}
        state = _state_from_payload(payload if isinstance(payload, dict) else {})
        phase = str(state.get("phase") or "")
        provider_id = str(state.get("provider_video_id") or "").strip()
        if phase == "provider_submitting" and not provider_id:
            held.add(int(row["id"]))
            logger(
                "[script-to-video] provider create outcome unknown; hold job=%s"
                % row["id"], flush=True,
            )
            continue
        safe = phase in {
            "preparing_materials", "materials_ready", "provider_submitted",
            "provider_completed", "composing", "done",
        }
        if safe and requeue(row["id"]):
            handled += 1
    return {"handled": handled, "held": held}


def _gen_talking(username, scenes, payload):
    """Freeze materials, submit once, then compose from durable local state."""
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
    job_id = payload.get("_job_id")
    runtime_managed = False
    current_payload = {}
    if job_id:
        try:
            current_payload, _ = _load_job_payload(job_id, username)
            runtime_managed = True
        except PermissionError:
            raise
        except Exception as exc:
            # Direct unit/library callers historically supplied a display-only
            # job id without a jobs database.  Real workers always have a
            # persisted row and therefore never take this compatibility path.
            if "不存在" not in str(exc) and "no such table" not in str(exc).lower():
                raise
    materials = (
        _prepare_frozen_materials(job_id, username, material_plan)
        if runtime_managed else _material_images(material_plan)
    )
    if runtime_managed:
        current_payload, _ = _load_job_payload(job_id, username)
    state = _state_from_payload(current_payload)
    phase = str(state.get("phase") or "")
    final_result = state.get("final_result") or {}
    if phase == "done" and isinstance(final_result, dict) and _provider_file_exists(final_result):
        return dict(final_result)
    if phase == "provider_submitting" and not state.get("provider_video_id"):
        raise ScriptToVideoRecoveryRequired(
            "供应商提交结果待核对，已停止重复提交"
        )

    result = _provider_base_result(state) if runtime_managed else {}
    if not _provider_file_exists(result):
        def on_prepared(data):
            _persist_job_state(
                job_id, username, "materials_ready",
                audio_file=data.get("audio_file"), image_file=data.get("image_file"),
            )

        def on_submitting(data):
            _persist_job_state(
                job_id, username, "provider_submitting",
                provider=data.get("provider"),
                image_asset_id=data.get("image_asset_id"),
                audio_asset_id=data.get("audio_asset_id"),
            )

        def on_submitted(data):
            _persist_job_state(
                job_id, username, "provider_submitted",
                provider=data.get("provider"),
                provider_video_id=data.get("provider_video_id"),
                image_asset_id=data.get("image_asset_id"),
                audio_asset_id=data.get("audio_asset_id"),
            )

        def on_rejected(_data):
            _persist_job_state(
                job_id, username, "materials_ready",
                provider=None, provider_video_id=None,
                image_asset_id=None, audio_asset_id=None,
            )
            video_domain.update_video_asset_phase(
                job_id, "materials_ready", strict=True,
            )

        def on_completed(data):
            provider_result = {
                key: data.get(key) for key in (
                    "video_id", "video_file", "video_url", "source_video_url",
                    "thumbnail_url", "duration", "provider", "image_file",
                    "image_url", "image_asset_id", "audio_asset_id",
                ) if data.get(key) is not None
            }
            _persist_job_state(
                job_id, username, "provider_completed",
                provider_video_id=data.get("video_id"),
                provider_result=provider_result,
            )
            video_domain.update_video_asset_phase(
                job_id, "provider_completed", strict=True,
                provider_video_id=data.get("video_id"),
                video_file=data.get("video_file"),
                source_video_url=data.get("source_video_url"),
            )

        lifecycle = {
            "state": state,
            "on_prepared": on_prepared,
            "on_submitting": on_submitting,
            "on_rejected": on_rejected,
            "on_submitted": on_submitted,
            "on_completed": on_completed,
        }
        video_payload = {
                "_username": username,
                "_job_id": job_id,
                "mode": "text",
                "text": full_text,
                "avatar_id": str(avatar["id"]),
                "voice": payload.get("voice") or "S_d21F8OR62",
                "resolution": payload.get("resolution") or "720p",
                "ratio": payload.get("ratio") or "9:16",
                "motion": payload.get("motion") or "medium",
                "motion_prompt": payload.get("motion_prompt") or "",
                "subtitle": False if material_plan else want_subtitle,
            }
        try:
            result = (
                video_domain.gen_video(video_payload, provider_lifecycle=lifecycle)
                if runtime_managed else video_domain.gen_video(video_payload)
            )
            if runtime_managed:
                _persist_job_state(
                    job_id, username, "provider_completed",
                    provider_video_id=result.get("provider_video_id"),
                    provider_result=result,
                )
        except video_domain.HeyGenProviderFailed:
            raise
        except BaseException as exc:
            latest = get_recovery_state(job_id) if runtime_managed else {}
            if str(latest.get("phase") or "") in {
                    "provider_submitting", "provider_submitted", "provider_completed"}:
                raise ScriptToVideoRecoveryRequired(str(exc)[:220]) from exc
            raise

    if runtime_managed:
        _persist_job_state(
            job_id, username, "composing",
            provider_video_id=(result.get("provider_video_id") or result.get("video_id")),
            provider_result=result,
        )
    result.setdefault("provider_video_file", result.get("video_file"))
    result.setdefault("provider_video_url", result.get("video_url"))
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
    except Exception as exc:
        raise ScriptToVideoRecoveryRequired(
            "基础成片已保留，本地合成待恢复: %s" % str(exc)[:180]
        ) from exc
    result.update({
        "type": "script_to_video",
        "scene_count": len(scenes),
        "pipeline": "talking_with_materials" if material_plan else "talking",
        "materials": materials,
        "material_generated_count": sum(1 for item in materials if item["source"] == "generate"),
        "material_reused_count": sum(1 for item in materials if item["source"] == "asset"),
    })
    if runtime_managed:
        _persist_job_state(
            job_id, username, "done",
            provider_video_id=(result.get("provider_video_id") or result.get("video_id")),
            final_result=result,
        )
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
