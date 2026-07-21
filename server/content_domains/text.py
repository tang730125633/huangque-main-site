# -*- coding: utf-8 -*-
import time
from contextlib import closing

from .core import COPY_MODEL, _post, json

_ILLEGAL_INTENT_PAIRS = (("假证", "教程"), ("假证", "操作"), ("绕过", "实名认证"), ("绕过", "风控"))


def validate_copy_payload(payload):
    prompt = str((payload or {}).get("prompt") or "").strip()
    if not prompt:
        raise ValueError("请输入文案需求")
    if len(prompt) > 6000:
        raise ValueError("文案需求过长，请精简后重试")
    if any(a in prompt and b in prompt for a, b in _ILLEGAL_INTENT_PAIRS):
        raise ValueError("内容可能涉及违法或规避平台安全措施，无法生成")
    return payload


def validate_copy_submission(payload, username, jdb):
    payload = dict(validate_copy_payload(payload))
    try: parent_job_id = int(payload.get("parent_job_id") or 0)
    except (TypeError, ValueError): raise ValueError("原脚本版本无效")
    if not parent_job_id:
        payload.pop("parent_job_id", None); payload["version"] = 1
        return payload
    with closing(jdb()) as c:
        row = c.execute("SELECT kind,username,status,result FROM jobs WHERE id=?", (parent_job_id,)).fetchone()
    if not row or row["username"] != username or row["kind"] != "copy" or row["status"] != "done":
        raise ValueError("原脚本版本不存在")
    try: previous = json.loads(row["result"] or "{}")
    except Exception: previous = {}
    if previous.get("mode") != "script": raise ValueError("原任务不是分镜脚本")
    payload["parent_job_id"] = parent_job_id
    payload["version"] = max(1, min(99, int(previous.get("version") or 1) + 1))
    return payload

def _chat(sysmsg, usermsg, temp):
    body = json.dumps({"model": COPY_MODEL,
                       "messages": [{"role": "system", "content": sysmsg}, {"role": "user", "content": usermsg}],
                       "temperature": temp}).encode()
    d = _post("/v1/chat/completions", body, "application/json")
    return (d.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()

def gen_copy(payload):
    payload = validate_copy_payload(payload)
    brief = (payload.get("prompt") or "").strip()
    ctype = (payload.get("ctype") or payload.get("type") or "通用").strip()
    # 编导：结构化分镜脚本（返回 scenes 数组）
    if (payload.get("format") or "") == "script":
        style = payload.get("style") or "口播"; dur = payload.get("dur") or "30s"; plat = payload.get("platform") or "抖音"
        industry = str(payload.get("industry") or "通用").strip()[:30] or "通用"
        raw = _chat("你是黄雀传媒资深短视频编导。只输出 JSON 本身，不要解释、不要 markdown 代码块。",
                    ("为%s行业的以下选题生成一套可拍的%s短视频分镜脚本（平台%s，总时长约%s）。\n选题/卖点：%s\n"
                     "严格输出 JSON：{\"scenes\":[{\"dur\":\"3s\",\"scene\":\"画面描述\",\"line\":\"口播台词\"}]}，"
                     "3-4 个分镜，各 dur 之和≈总时长，口播口语化有钩子可直接念。" % (industry, style, plat, dur, brief)), 0.85)
        s, e = raw.find("{"), raw.rfind("}"); scenes = []
        if s >= 0 and e > s:
            try: scenes = json.loads(raw[s:e+1]).get("scenes", [])
            except Exception: scenes = []
        if not scenes: raise ValueError("脚本解析失败，请重试")
        try: parent_job_id = int(payload.get("parent_job_id") or 0) or None
        except (TypeError, ValueError): parent_job_id = None
        try: version = max(1, min(99, int(payload.get("version") or 1)))
        except (TypeError, ValueError): version = 1
        return {"type": "copy", "mode": "script", "scenes": scenes, "ctype": ctype,
                "style": style, "dur": dur, "platform": plat, "prompt": brief,
                "industry": industry, "parent_job_id": parent_job_id, "version": version}
    # 通用文案（多条，--- 分隔）
    try: n = max(1, min(3, int(payload.get("n") or 2)))
    except Exception: n = 2
    text = _chat("你是黄雀传媒资深美业/电商营销文案。输出简体中文，口语化、有钩子、能转化。直接给文案本身，不要任何解释说明、不要前后缀。",
                 ("文案类型：%s\n需求/主题：%s\n请给 %d 条不同风格的文案，每条之间用单独一行「---」分隔；可适当用 emoji 和话题标签。" % (ctype, brief, n)), 0.9)
    if not text: raise ValueError("文案生成为空")
    return {"type": "copy", "ctype": ctype, "text": text, "prompt": brief}

HANDLERS = {"copy": gen_copy}


def _clean_scenes(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 12:
        raise ValueError("脚本必须包含 1～12 个分镜")
    scenes = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("分镜格式无效")
        scene = str(item.get("scene") or "").strip()[:2000]
        line = str(item.get("line") or "").strip()[:2000]
        dur = str(item.get("dur") or "").strip()[:20]
        if not scene and not line:
            raise ValueError("分镜画面和口播不能同时为空")
        scenes.append({"dur": dur, "scene": scene, "line": line})
    return scenes


def update_script(jdb, username, job_id, payload):
    """Persist edits only to the owner's completed copy/script result."""
    with closing(jdb()) as c:
        row = c.execute("SELECT kind,username,status,result FROM jobs WHERE id=?", (int(job_id),)).fetchone()
        if not row or row["username"] != username or row["kind"] != "copy" or row["status"] != "done":
            raise PermissionError("脚本不存在")
        try: result = json.loads(row["result"] or "{}")
        except Exception: result = {}
        if result.get("mode") != "script":
            raise ValueError("该任务不是分镜脚本")
        result["scenes"] = _clean_scenes((payload or {}).get("scenes"))
        result["edited_at"] = int(time.time())
        c.execute("UPDATE jobs SET result=?,updated_at=? WHERE id=?",
                  (json.dumps(result, ensure_ascii=False), result["edited_at"], int(job_id)))
        c.commit()
        return result


def handle_update(handler, user, jdb, assets_store):
    if not user:
        return handler._send(401, {"detail": "未登录"})
    try:
        jid = int(handler.path.split("?")[0].rsplit("/", 1)[1])
        result = update_script(jdb, user["username"], jid, handler._json_body_strict())
        assets_store.update_copy_asset(jid, user["username"], result)
        return handler._send(200, {"ok": True, "job_id": jid, "result": result})
    except PermissionError:
        return handler._send(404, {"detail": "脚本不存在"})
    except ValueError as exc:
        return handler._send(400, {"detail": str(exc)[:220]})
