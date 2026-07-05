# -*- coding: utf-8 -*-
from . import core as _core
globals().update({k: getattr(_core, k) for k in dir(_core) if not k.startswith("__")})

def get_user_id(username):
    try:
        q = urllib.parse.quote(str(username or ""), safe="")
        res = _auth_points_request("/api/auth/points?username=" + q, method="GET")
        return (res.get("user") or {}).get("user_id")
    except Exception:
        return None

def assign_audio_voice_slot(username):
    username = (username or "").strip()
    if not username:
        raise ValueError("missing username")
    user_id = get_user_id(username)
    now = int(time.time())
    with closing(adb()) as c:
        c.execute("BEGIN IMMEDIATE")
        slot = c.execute("""SELECT slot_id FROM voice_slot_pool
            WHERE status='available'
            ORDER BY created_at, slot_id
            LIMIT 1""").fetchone()
        if not slot:
            c.rollback()
            raise ValueError("\u6682\u65e0\u53ef\u5206\u914d\u7684\u97f3\u8272\u69fd\u4f4d")
        slot_id = slot["slot_id"]
        cur = c.execute("""UPDATE voice_slot_pool
            SET status='assigned', assigned_user_id=?, assigned_username=?, assigned_at=?
            WHERE slot_id=? AND status='available'""", (user_id, username, now, slot_id))
        if cur.rowcount != 1:
            c.rollback()
            raise ValueError("\u69fd\u4f4d\u5206\u914d\u51b2\u7a81\uff0c\u8bf7\u91cd\u8bd5")
        c.execute("""INSERT INTO audio_voice_slots
            (username, user_id, slot_id, status, created_at, updated_at)
            VALUES(?,?,?,?,?,?)""", (username, user_id, slot_id, "active", now, now))
        c.commit()
        return {"slot_id": slot_id, "username": username, "user_id": user_id, "status": "active"}

def redeem_audio_voice_slot(username, code):
    username = (username or "").strip()
    code = (code or "").strip()
    if not username:
        raise ValueError("missing username")
    if not code:
        raise ValueError("\u8bf7\u8f93\u5165\u5151\u6362\u7801")
    user_id = get_user_id(username)
    now = int(time.time())
    with closing(adb()) as c:
        c.execute("BEGIN IMMEDIATE")
        rc = c.execute("""SELECT code, status FROM voice_slot_codes
            WHERE code=?""", (code,)).fetchone()
        if not rc:
            c.rollback()
            raise ValueError("\u5151\u6362\u7801\u4e0d\u5b58\u5728")
        if rc["status"] != "unused":
            c.rollback()
            raise ValueError("\u5151\u6362\u7801\u5df2\u4f7f\u7528\u6216\u5df2\u5931\u6548")
        slot = c.execute("""SELECT slot_id FROM voice_slot_pool
            WHERE status='available'
            ORDER BY created_at, slot_id
            LIMIT 1""").fetchone()
        if not slot:
            c.rollback()
            raise ValueError("\u6682\u65e0\u53ef\u5206\u914d\u7684\u97f3\u8272\u69fd\u4f4d")
        slot_id = slot["slot_id"]
        cur = c.execute("""UPDATE voice_slot_pool
            SET status='assigned', assigned_user_id=?, assigned_username=?, assigned_at=?
            WHERE slot_id=? AND status='available'""", (user_id, username, now, slot_id))
        if cur.rowcount != 1:
            c.rollback()
            raise ValueError("\u69fd\u4f4d\u5206\u914d\u51b2\u7a81\uff0c\u8bf7\u91cd\u8bd5")
        c.execute("""INSERT INTO audio_voice_slots
            (username, user_id, slot_id, status, created_at, updated_at)
            VALUES(?,?,?,?,?,?)""", (username, user_id, slot_id, "active", now, now))
        cur = c.execute("""UPDATE voice_slot_codes
            SET status='used', assigned_slot_id=?, used_user_id=?, used_username=?, used_at=?
            WHERE code=? AND status='unused'""", (slot_id, user_id, username, now, code))
        if cur.rowcount != 1:
            c.rollback()
            raise ValueError("\u5151\u6362\u7801\u72b6\u6001\u66f4\u65b0\u5931\u8d25")
        c.commit()
        return {"slot_id": slot_id, "username": username, "user_id": user_id, "status": "active"}

def list_user_audio_voice_slots(username):
    with closing(adb()) as c:
        rows = c.execute("""SELECT s.id, s.username, s.user_id, s.slot_id, s.status, s.voice_id, COALESCE(s.reclone_count, 0) AS reclone_count,
                   s.created_at, s.updated_at, s.clone_started_at, s.clone_upload_at, s.clone_error,
                   s.clone_upload_speaker_id, s.clone_upload_response,
                   v.display_name AS voice_name, v.preview_file, v.preview_url, v.updated_at AS voice_updated_at
            FROM audio_voice_slots s
            LEFT JOIN audio_voices v ON v.id = s.voice_id
            WHERE s.username=?
            ORDER BY s.id DESC""", (username,)).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        if d.get("slot_id") and d.get("status") in ("training", "ready") and not d.get("preview_url"):
            try:
                synced = check_clone_status(username, d["slot_id"])
                if synced.get("status"):
                    d["status"] = synced["status"]
                if synced.get("preview_url"):
                    d["preview_url"] = synced["preview_url"]
            except Exception as e:
                print("[list_user_audio_voice_slots] sync failed username=%s slot_id=%s error=%s" %
                      (username, d.get("slot_id"), str(e)[:240]), flush=True)
        if d.get("preview_url") and d.get("voice_id") and d.get("status") == "training":
            d["status"] = "ready"
        items.append(d)
    return items

def generate_doubao_preview(speaker_id, text=None, speech_rate=0, loudness_rate=0, pitch_rate=0):
    text = (text or "\u4f60\u597d\uff0c\u8fd9\u662f\u6211\u7684\u4e13\u5c5e\u590d\u523b\u97f3\u8272\u8bd5\u542c\u3002\u58f0\u97f3\u6e05\u6670\u81ea\u7136\uff0c\u9002\u5408\u7528\u4e8e\u77ed\u89c6\u9891\u53e3\u64ad\u548c\u6587\u6848\u914d\u97f3\u3002").strip()
    reqid = "hq_preview_%d" % int(time.time() * 1000)
    body = json.dumps({
        "user": {"uid": "huangque"},
        "req_params": {
            "text": text,
            "speaker": speaker_id,
            "audio_params": {
                "format": "mp3",
                "sample_rate": 24000,
                "speech_rate": int(speech_rate or 0),
                "loudness_rate": int(loudness_rate or 0),
                "pitch_rate": int(pitch_rate or 0),
            },
            "additions": json.dumps({"explicit_language": "zh", "disable_markdown_filter": True}),
        },
    }, ensure_ascii=False).encode()
    req = urllib.request.Request("https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Api-App-Id": DOUBAO_APPID,
            "X-Api-Access-Key": DOUBAO_TOKEN,
            "X-Api-Resource-Id": DOUBAO_TTS_RESOURCE,
            "X-Api-Request-Id": reqid,
        },
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise ValueError("\u8bd5\u542c\u97f3\u9891\u751f\u6210\u5931\u8d25: " + detail)
    chunks = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(b"data:"):
            line = line[5:].strip()
        elif line.startswith(b"event:"):
            continue
        try:
            d = json.loads(line.decode("utf-8", "replace"))
        except Exception:
            continue
        if d.get("code") == 20000000:
            break
        data = d.get("data") or d.get("audio") or d.get("audio_data")
        if isinstance(data, str) and data:
            try:
                chunks.append(base64.b64decode(data))
            except Exception:
                pass
        if d.get("code") not in (None, 0, 20000000) or d.get("error") or d.get("message") == "error":
            raise ValueError(json.dumps(d, ensure_ascii=False)[:200])
    if not chunks:
        try:
            d = json.loads(raw.decode("utf-8", "replace"))
            data = d.get("data") or d.get("audio") or d.get("audio_data")
            if isinstance(data, str) and data:
                chunks.append(base64.b64decode(data))
        except Exception:
            pass
    if not chunks:
        raise ValueError("\u8bd5\u542c\u97f3\u9891\u751f\u6210\u8fd4\u56de\u4e3a\u7a7a")
    fn = "audio/voice_preview_%d.mp3" % int(time.time() * 1000)
    _out_path(fn).write_bytes(b"".join(chunks))
    return {"file": fn, "url": _file_url(fn), "text": text}

def query_doubao_clone_status(slot_id):
    body = json.dumps({"appid": DOUBAO_APPID, "speaker_id": slot_id}).encode()
    req = urllib.request.Request("https://openspeech.bytedance.com/api/v1/mega_tts/status",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer;" + DOUBAO_TOKEN,
            "Resource-Id": DOUBAO_CLONE_RESOURCE,
        },
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8", "replace")
            resp = json.loads(raw or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise ValueError("\u8c46\u5305\u590d\u523b\u72b6\u6001\u67e5\u8be2\u5931\u8d25: " + detail)
    base = resp.get("BaseResp") or resp.get("base_resp") or {}
    code = base.get("StatusCode", base.get("status_code", resp.get("code", 0)))
    try:
        code_i = int(code)
    except Exception:
        code_i = 0 if code in ("0", "OK", "ok", None) else -1
    if code_i not in (0,):
        msg = base.get("StatusMessage") or base.get("status_message") or resp.get("message") or json.dumps(resp, ensure_ascii=False)[:200]
        raise ValueError("\u8c46\u5305\u590d\u523b\u72b6\u6001\u5f02\u5e38: " + str(msg)[:200])
    return resp

def finalize_ready_voice(username, slot_id, display_name=None, demo_audio=None, preview_file=None):
    now = int(time.time())
    voice_key = "vip_" + re.sub(r"[^a-zA-Z0-9_\\-]", "_", slot_id)
    name = (display_name or "\u6211\u7684VIP\u590d\u523b\u97f3\u8272").strip()[:40]
    with closing(adb()) as c:
        c.execute("""INSERT OR IGNORE INTO audio_voices
            (username, scope, voice_key, display_name, provider_voice, preview_file, preview_url, slot_id, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (username, "personal", voice_key, name, slot_id, preview_file, demo_audio, slot_id, now, now))
        c.execute("""UPDATE audio_voices
            SET display_name=?, provider_voice=?, preview_file=?, preview_url=?, slot_id=?, updated_at=?
            WHERE username=? AND scope='personal' AND voice_key=?""",
            (name, slot_id, preview_file, demo_audio, slot_id, now, username, voice_key))
        r = c.execute("SELECT id FROM audio_voices WHERE username=? AND scope='personal' AND voice_key=?",
                      (username, voice_key)).fetchone()
        voice_id = r["id"] if r else None
        c.execute("""UPDATE audio_voice_slots SET voice_id=?, status='ready', clone_started_at=NULL, previous_preview_url=NULL, clone_error=NULL, updated_at=?
            WHERE username=? AND slot_id=?""", (voice_id, now, username, slot_id))
        c.commit()
    return {"voice_id": voice_id, "voice_key": voice_key, "display_name": name, "preview_file": preview_file, "preview_url": demo_audio, "status": "ready"}

def clear_voice_preview(username, slot_id):
    username = (username or "").strip()
    slot_id = (slot_id or "").strip()
    if not username or not slot_id:
        return 0
    removed = 0
    with closing(adb()) as c:
        rows = c.execute("""SELECT id, preview_file, preview_url FROM audio_voices
            WHERE username=? AND slot_id=?""", (username, slot_id)).fetchall()
        for r in rows:
            refs = []
            if r["preview_file"]:
                refs.append(str(r["preview_file"]))
            url = r["preview_url"] or ""
            if url.startswith("/api/gen/file/"):
                refs.append(url[len("/api/gen/file/"):])
            for ref in refs:
                name = os.path.basename(str(ref))
                if name.startswith("voice_preview_") and name.endswith(".mp3"):
                    fp = _resolve_out_file(ref)
                    try:
                        if fp and fp.exists():
                            fp.unlink()
                            removed += 1
                    except Exception as e:
                        print("[clear_voice_preview] delete failed file=%s error=%s" % (name, str(e)[:200]), flush=True)
        c.execute("""UPDATE audio_voices SET preview_file=NULL, preview_url=NULL, updated_at=?
            WHERE username=? AND slot_id=?""", (int(time.time()), username, slot_id))
        c.commit()
    return removed

def check_clone_status(username, slot_id):
    username = (username or "").strip()
    slot_id = (slot_id or "").strip()
    with closing(adb()) as c:
        slot = c.execute("""SELECT id, slot_id, status, voice_id, clone_started_at, clone_upload_at, clone_error,
                   clone_baseline_version, clone_baseline_icl_speaker_id, clone_baseline_demo_audio
            FROM audio_voice_slots
            WHERE username=? AND slot_id=?""", (username, slot_id)).fetchone()
        voice = c.execute("""SELECT display_name, preview_url FROM audio_voices
            WHERE username=? AND slot_id=? ORDER BY id DESC LIMIT 1""", (username, slot_id)).fetchone()
    if not slot:
        raise ValueError("\u97f3\u8272\u69fd\u4f4d\u4e0d\u5b58\u5728\u6216\u4e0d\u5c5e\u4e8e\u5f53\u524d\u8d26\u53f7")
    if slot["status"] == "failed":
        return {"status": "failed", "clone_error": slot["clone_error"] or "\u8c46\u5305\u590d\u523b\u5931\u8d25", "doubao_status": None}
    if slot["status"] == "ready" and voice and voice["preview_url"] and not slot["clone_started_at"]:
        return {"status": "ready", "preview_url": voice["preview_url"], "doubao_status": 2}
    try:
        resp = query_doubao_clone_status(slot_id)
    except Exception:
        if slot["status"] == "training":
            return {"status": "training", "doubao_status": None}
        raise
    st = resp.get("status")
    demo = resp.get("demo_audio")
    version = str(resp.get("version") or "")
    icl_speaker_id = str(resp.get("icl_speaker_id") or "")
    create_time = resp.get("create_time") or resp.get("createTime") or resp.get("created_at")
    try:
        create_time_i = int(create_time or 0)
    except Exception:
        create_time_i = 0
    clone_started_at = int(slot["clone_started_at"] or 0)
    clone_upload_at = int(slot["clone_upload_at"] or 0)
    baseline_version = str(slot["clone_baseline_version"] or "")
    baseline_icl = str(slot["clone_baseline_icl_speaker_id"] or "")
    baseline_demo = str(slot["clone_baseline_demo_audio"] or "")
    same_as_baseline = bool(baseline_version or baseline_icl or baseline_demo) and (
        (not baseline_version or version == baseline_version) and
        (not baseline_icl or icl_speaker_id == baseline_icl) and
        (not baseline_demo or str(demo or "") == baseline_demo)
    )
    if st == 2 and same_as_baseline:
        return {
            "status": "training",
            "doubao_status": st,
            "doubao_create_time": create_time_i,
            "doubao_version": version,
            "doubao_icl_speaker_id": icl_speaker_id,
            "clone_started_at": clone_started_at,
            "clone_upload_at": clone_upload_at,
            "stale_result": True,
        }
    if st == 2:
        try:
            preview = generate_doubao_preview(slot_id)
            preview_url = preview.get("url")
            preview_file = preview.get("file")
        except Exception as e:
            err = "\u6d4b\u8bd5\u97f3\u9891\u751f\u6210\u5931\u8d25: " + str(e)[:220]
            print("[check_clone_status] preview tts failed username=%s slot_id=%s error=%s" %
                  (username, slot_id, str(e)[:240]), flush=True)
            with closing(adb()) as c:
                c.execute("UPDATE audio_voice_slots SET status='failed', clone_error=?, updated_at=? WHERE username=? AND slot_id=?",
                          (err, int(time.time()), username, slot_id))
                c.commit()
            return {"status": "failed", "clone_error": err, "doubao_status": st, "doubao_demo_audio": demo}
        if not preview_url:
            err = "\u6d4b\u8bd5\u97f3\u9891\u751f\u6210\u8fd4\u56de\u4e3a\u7a7a"
            with closing(adb()) as c:
                c.execute("UPDATE audio_voice_slots SET status='failed', clone_error=?, updated_at=? WHERE username=? AND slot_id=?",
                          (err, int(time.time()), username, slot_id))
                c.commit()
            return {"status": "failed", "clone_error": err, "doubao_status": st, "doubao_demo_audio": demo}
        v = finalize_ready_voice(username, slot_id, voice["display_name"] if voice else None, preview_url, preview_file)
        return {"status": "ready", "preview_url": preview_url, "voice": v, "doubao_status": st, "doubao_demo_audio": demo, "doubao_create_time": create_time_i}
    if st == 3:
        with closing(adb()) as c:
            c.execute("UPDATE audio_voice_slots SET status='failed', updated_at=? WHERE username=? AND slot_id=?",
                      (int(time.time()), username, slot_id))
            c.commit()
        return {"status": "failed", "doubao_status": st}
    return {"status": "training", "doubao_status": st}

ALLOWED_CLONE_AUDIO_FORMATS = {"mp3", "wav", "m4a", "aac", "ogg"}

class CloneVipValidationError(ValueError):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status
        self.detail = detail

def _clone_audio_format(audio_format):
    return (audio_format or "mp3").strip().lower().lstrip(".")

def _clone_audio_b64(audio):
    audio = (audio or "").strip()
    if "," in audio:
        audio = audio.split(",", 1)[1].strip()
    return audio

def validate_clone_vip_payload(username, payload):
    username = (username or "").strip()
    if not isinstance(payload, dict):
        raise CloneVipValidationError(400, "请求体不是合法 JSON")
    slot_id = (payload.get("slot_id") or "").strip()
    if not slot_id:
        raise CloneVipValidationError(400, "缺少音色槽位 ID")
    audio_b64 = _clone_audio_b64(payload.get("audio"))
    if not audio_b64:
        raise CloneVipValidationError(400, "请先上传样音")
    audio_format = _clone_audio_format(payload.get("audio_format"))
    if audio_format not in ALLOWED_CLONE_AUDIO_FORMATS:
        raise CloneVipValidationError(400, "audio_format 仅支持 mp3/wav/m4a/aac/ogg")
    try:
        base64.b64decode(audio_b64, validate=True)
    except Exception:
        raise CloneVipValidationError(400, "样音不是有效的 base64 音频")
    with closing(adb()) as c:
        slot = c.execute("""SELECT id, status, voice_id, COALESCE(reclone_count, 0) AS reclone_count,
                updated_at, clone_upload_at
            FROM audio_voice_slots
            WHERE username=? AND slot_id=?""", (username, slot_id)).fetchone()
    if not slot:
        raise CloneVipValidationError(404, "音色槽位不存在或不属于当前账号")
    now = int(time.time())
    if slot["status"] == "training":
        last_at = int(slot["clone_upload_at"] or slot["updated_at"] or 0)
        if last_at and now - last_at < 600:
            raise CloneVipValidationError(409, "音色正在复刻中，请等待完成")
    is_reclone = slot["status"] == "ready" and bool(slot["voice_id"])
    reclone_count = int(slot["reclone_count"] or 0)
    if is_reclone and reclone_count >= 10:
        raise CloneVipValidationError(409, "该槽位已达复刻上限")
    checked = dict(payload)
    checked["slot_id"] = slot_id
    checked["audio"] = audio_b64
    checked["audio_format"] = audio_format
    return checked

def mark_clone_training(username, slot_id, name):
    username = (username or "").strip()
    slot_id = (slot_id or "").strip()
    name = (name or "\u6211\u7684VIP\u590d\u523b\u97f3\u8272").strip()[:40]
    now = int(time.time())
    voice_key = "vip_" + re.sub(r"[^a-zA-Z0-9_\\-]", "_", slot_id)
    with closing(adb()) as c:
        slot = c.execute("""SELECT id, status, voice_id, COALESCE(reclone_count, 0) AS reclone_count, updated_at, clone_upload_at FROM audio_voice_slots
            WHERE username=? AND slot_id=?""",
            (username, slot_id)).fetchone()
        if not slot:
            raise ValueError("\u97f3\u8272\u69fd\u4f4d\u4e0d\u5b58\u5728\u6216\u4e0d\u5c5e\u4e8e\u5f53\u524d\u8d26\u53f7")
        if slot["status"] == "training":
            last_at = int(slot["clone_upload_at"] or slot["updated_at"] or 0)
            if last_at and now - last_at < 600:
                raise ValueError("\u97f3\u8272\u6b63\u5728\u590d\u523b\u4e2d\uff0c\u8bf7\u7b49\u5f85\u5b8c\u6210")
        is_reclone = slot["status"] == "ready" and bool(slot["voice_id"])
        reclone_count = int(slot["reclone_count"] or 0)
        if is_reclone and reclone_count >= 10:
            raise ValueError("\u8be5\u97f3\u8272\u5df2\u8fbe\u5230\u6700\u9ad810\u6b21\u91cd\u65b0\u590d\u523b\u4e0a\u9650")
        next_reclone_count = reclone_count + 1 if is_reclone else reclone_count
        c.execute("""INSERT OR IGNORE INTO audio_voices
            (username, scope, voice_key, display_name, provider_voice, slot_id, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (username, "personal", voice_key, name, slot_id, slot_id, now, now))
        c.execute("""UPDATE audio_voices
            SET display_name=?, provider_voice=?, slot_id=?, updated_at=?
            WHERE username=? AND scope='personal' AND voice_key=?""",
            (name, slot_id, slot_id, now, username, voice_key))
        r = c.execute("SELECT id FROM audio_voices WHERE username=? AND scope='personal' AND voice_key=?",
                      (username, voice_key)).fetchone()
        voice_id = r["id"] if r else None
        c.execute("""UPDATE audio_voice_slots SET voice_id=?, status='training', reclone_count=?, clone_started_at=?, clone_upload_at=NULL, clone_error=NULL, updated_at=?
            WHERE username=? AND slot_id=?""", (voice_id, next_reclone_count, now, now, username, slot_id))
        c.commit()
    clear_voice_preview(username, slot_id)
    return {"voice_id": voice_id, "voice_key": voice_key, "display_name": name, "status": "training", "reclone_count": next_reclone_count, "reclone_remaining": max(0, 10 - next_reclone_count)}

def clone_vip_voice_background(username, payload):
    try:
        clone_vip_voice(username, payload)
    except Exception as e:
        slot_id = (payload.get("slot_id") or "").strip()
        print("[clone_vip_voice_background] failed username=%s slot_id=%s error=%s" % (username, slot_id, str(e)[:300]), flush=True)
        if slot_id:
            try:
                with closing(adb()) as c:
                    c.execute("UPDATE audio_voice_slots SET status='failed', updated_at=? WHERE username=? AND slot_id=?",
                              (int(time.time()), username, slot_id))
                    c.commit()
            except Exception:
                pass

def prepare_clone_audio(audio_b64, audio_format):
    raw = base64.b64decode(audio_b64)
    ts = int(time.time() * 1000)
    safe_format = re.sub(r"[^a-zA-Z0-9]", "", audio_format or "mp3")[:8] or "mp3"
    src = _out_path("audio/clone_src_%d.%s" % (ts, safe_format))
    dst = _out_path("audio/clone_60s_%d.mp3" % ts)
    src.write_bytes(raw)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-t", "60",
        "-ac", "1",
        "-ar", "16000",
        "-b:a", "48k",
        str(dst),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=120, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        data = dst.read_bytes()
    except Exception:
        data = raw
        dst = src
    finally:
        try:
            if src.exists() and src != dst:
                src.unlink()
        except Exception:
            pass
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("\u6837\u97f3\u6587\u4ef6\u8fc7\u5927\uff0c\u8bf7\u4e0a\u4f20\u66f4\u77ed\u6216\u66f4\u4f4e\u7801\u7387\u7684\u97f3\u9891")
    return base64.b64encode(data).decode(), "mp3"

def clone_vip_voice(username, payload):
    username = (username or "").strip()
    slot_id = (payload.get("slot_id") or "").strip()
    name = (payload.get("name") or "\u6211\u7684VIP\u590d\u523b\u97f3\u8272").strip()[:40]
    audio_b64 = _clone_audio_b64(payload.get("audio"))
    audio_format = _clone_audio_format(payload.get("audio_format"))
    if audio_format not in ALLOWED_CLONE_AUDIO_FORMATS:
        raise ValueError("audio_format 仅支持 mp3/wav/m4a/aac/ogg")
    if not slot_id:
        raise ValueError("\u7f3a\u5c11\u97f3\u8272\u69fd\u4f4d")
    if not audio_b64:
        raise ValueError("\u8bf7\u5148\u4e0a\u4f20\u6837\u97f3")
    if not DOUBAO_APPID or not DOUBAO_TOKEN:
        raise ValueError("\u8c46\u5305\u58f0\u97f3\u590d\u523b\u914d\u7f6e\u672a\u5b8c\u6210")
    with closing(adb()) as c:
        slot = c.execute("""SELECT id, slot_id, voice_id FROM audio_voice_slots
            WHERE username=? AND slot_id=? AND status IN ('active','training','failed','ready')""", (username, slot_id)).fetchone()
    if not slot:
        raise ValueError("\u97f3\u8272\u69fd\u4f4d\u4e0d\u5b58\u5728\u6216\u4e0d\u5c5e\u4e8e\u5f53\u524d\u8d26\u53f7")
    audio_b64, audio_format = prepare_clone_audio(audio_b64, audio_format)
    baseline_version = baseline_icl = baseline_demo = ""
    try:
        baseline = query_doubao_clone_status(slot_id)
        baseline_version = str(baseline.get("version") or "")
        baseline_icl = str(baseline.get("icl_speaker_id") or "")
        baseline_demo = str(baseline.get("demo_audio") or "")
    except Exception as e:
        print("[clone_vip_voice] baseline status skipped username=%s slot_id=%s error=%s" %
              (username, slot_id, str(e)[:200]), flush=True)
    body = json.dumps({
        "appid": DOUBAO_APPID,
        "speaker_id": slot_id,
        "audios": [{"audio_bytes": audio_b64, "audio_format": audio_format}],
        "source": 2,
        "language": 0,
        "model_type": DOUBAO_CLONE_MODEL_TYPE,
    }).encode()
    req = urllib.request.Request("https://openspeech.bytedance.com/api/v1/mega_tts/audio/upload",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer;" + DOUBAO_TOKEN,
            "Resource-Id": DOUBAO_CLONE_RESOURCE,
        },
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = r.read().decode("utf-8", "replace")
            resp = json.loads(raw or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise ValueError("\u8c46\u5305VIP\u590d\u523b\u63a5\u53e3\u5931\u8d25: " + detail)
    except Exception as e:
        raise ValueError("\u8c46\u5305VIP\u590d\u523b\u8bf7\u6c42\u5931\u8d25: " + str(e)[:160])
    base = resp.get("BaseResp") or resp.get("base_resp") or {}
    code = base.get("StatusCode", base.get("status_code", resp.get("code", 0)))
    try:
        code_i = int(code)
    except Exception:
        code_i = 0 if code in ("0", "OK", "ok", None) else -1
    if code_i not in (0,):
        msg = base.get("StatusMessage") or base.get("status_message") or resp.get("message") or json.dumps(resp, ensure_ascii=False)[:200]
        raise ValueError("\u8c46\u5305VIP\u590d\u523b\u5931\u8d25: " + str(msg)[:200])
    returned_speaker_id = (resp.get("speaker_id") or resp.get("speakerId") or "").strip()
    if returned_speaker_id and returned_speaker_id != slot_id:
        raise ValueError("\u8c46\u5305VIP\u590d\u523b\u8fd4\u56de\u7684\u97f3\u8272ID\u4e0e\u69fd\u4f4dID\u4e0d\u4e00\u81f4")
    upload_resp = json.dumps({
        "BaseResp": base,
        "speaker_id": returned_speaker_id,
        "message": resp.get("message"),
        "code": resp.get("code"),
    }, ensure_ascii=False)[:1000]
    now = int(time.time())
    voice_key = "vip_" + re.sub(r"[^a-zA-Z0-9_\\-]", "_", slot_id)
    with closing(adb()) as c:
        c.execute("""INSERT OR IGNORE INTO audio_voices
            (username, scope, voice_key, display_name, provider_voice, slot_id, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (username, "personal", voice_key, name, slot_id, slot_id, now, now))
        c.execute("""UPDATE audio_voices
            SET display_name=?, provider_voice=?, preview_file=NULL, preview_url=NULL, slot_id=?, updated_at=?
            WHERE username=? AND scope='personal' AND voice_key=?""",
            (name, slot_id, slot_id, now, username, voice_key))
        r = c.execute("SELECT id FROM audio_voices WHERE username=? AND scope='personal' AND voice_key=?",
                      (username, voice_key)).fetchone()
        voice_id = r["id"] if r else None
        c.execute("""UPDATE audio_voice_slots
            SET voice_id=?, status='training', clone_started_at=?, clone_upload_at=?, clone_error=NULL,
                clone_upload_speaker_id=?, clone_upload_response=?,
                clone_baseline_version=?, clone_baseline_icl_speaker_id=?, clone_baseline_demo_audio=?,
                updated_at=?
            WHERE username=? AND slot_id=?""",
            (voice_id, now, now, returned_speaker_id, upload_resp,
             baseline_version, baseline_icl, baseline_demo, now, username, slot_id))
        c.commit()
    print("[clone_vip_voice] upload ok username=%s slot_id=%s returned_speaker_id=%s response=%s" %
          (username, slot_id, returned_speaker_id or "", upload_resp[:500]), flush=True)
    return {"voice_id": voice_id, "voice_key": voice_key, "display_name": name, "status": "training", "speaker_id": returned_speaker_id or slot_id}

def ensure_audio_voice(username, voice_key):
    username = (username or "").strip()
    voice_key = (voice_key or "S_d21F8OR62").strip()
    public_keys = set()  # dapeng/zelong/paul removed
    public_key = voice_key.lower()
    now = int(time.time())
    with closing(adb()) as c:
        r = c.execute("SELECT id FROM audio_voices WHERE scope='public' AND username='' AND voice_key=?",
                      (voice_key,)).fetchone()
        if r: return r["id"]
    if public_key in public_keys:
        voice_key = public_key
        with closing(adb()) as c:
            r = c.execute("SELECT id FROM audio_voices WHERE scope='public' AND username='' AND voice_key=?",
                          (voice_key,)).fetchone()
            if r: return r["id"]
            display = voice_key
            cur = c.execute("""INSERT INTO audio_voices
                (scope, username, voice_key, display_name, provider_voice, created_at, updated_at)
                VALUES('public','',?,?,?,?,?)""",
                (voice_key, display, VOICE_MAP.get(voice_key, "alloy"), now, now))
            c.commit()
            return cur.lastrowid
    with closing(adb()) as c:
        r = c.execute("SELECT id FROM audio_voices WHERE scope='personal' AND username=? AND voice_key=?",
                      (username, voice_key)).fetchone()
        if r: return r["id"]
        display = "\u6211\u7684\u590d\u523b\u97f3\u8272" if voice_key == "personal" else voice_key
        cur = c.execute("""INSERT INTO audio_voices
            (scope, username, voice_key, display_name, provider_voice, created_at, updated_at)
            VALUES('personal',?,?,?,?,?,?)""",
            (username, voice_key, display, VOICE_MAP.get(voice_key, VOICE_MAP.get("personal", "alloy")), now, now))
        c.commit()
        return cur.lastrowid

def resolve_audio_provider_voice(username, voice_key):
    username = (username or "").strip()
    voice_key = (voice_key or "S_d21F8OR62").strip()
    public_keys = set()  # dapeng/zelong/paul removed
    public_key = voice_key.lower()
    with closing(adb()) as c:
        r = c.execute("""SELECT provider_voice FROM audio_voices
            WHERE scope='public' AND username='' AND voice_key=?""",
            (voice_key,)).fetchone()
    if r:
        return r["provider_voice"]
    if public_key in public_keys:
        ensure_audio_voice(username, public_key)
        return VOICE_MAP.get(public_key, "alloy")
    if voice_key == "personal":
        ensure_audio_voice(username, voice_key)
        return VOICE_MAP.get("personal", "alloy")
    with closing(adb()) as c:
        r = c.execute("""SELECT provider_voice FROM audio_voices
            WHERE scope='personal' AND username=? AND voice_key=?""",
            (username, voice_key)).fetchone()
    if not r:
        raise ValueError("个人音色不存在或不属于当前账号")
    return r["provider_voice"]

def record_audio_asset(job_id, username, result):
    if not result or result.get("type") != "audio":
        return
    now = int(time.time())
    raw_voice_key = (result.get("voice") or "S_d21F8OR62").strip()
    voice_key = raw_voice_key.lower() if raw_voice_key.lower() in set() else raw_voice_key
    voice_id = ensure_audio_voice(username, voice_key)
    with closing(adb()) as c:
        c.execute("""INSERT OR REPLACE INTO audio_assets
            (job_id, username, voice_id, voice_key, file, url, text, speed, pitch, volume, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, username, voice_id, voice_key, result.get("file"), result.get("url"),
             result.get("text") or result.get("prompt"), result.get("speed"), result.get("pitch"),
             result.get("volume"), now))
        c.commit()

def backfill_audio_assets():
    try:
        with closing(jdb()) as c:
            rows = c.execute("""SELECT id, username, result FROM jobs
                WHERE kind='audio' AND status='done' AND result IS NOT NULL""").fetchall()
        for r in rows:
            try:
                record_audio_asset(r["id"], r["username"], json.loads(r["result"]))
            except Exception:
                pass
    except Exception:
        pass

PUBLIC_VOICE_SAMPLE_TEXT = "大家好，这是我的声音示范，很高兴为你服务。"

def _ensure_public_voice_preview(row):
    """公共音色缺 preview_url 时懒生成一段试听样音、上 COS、回填 DB。
    幂等：只在 preview_url 为空时合成，生成后写回，后续走缓存。
    非致命：合成失败不影响音色列表返回（该音色本次暂无 ▶，下次再试）。"""
    d = dict(row)
    if d.get("scope") != "public" or d.get("preview_url"):
        return d
    speaker = (d.get("provider_voice") or d.get("voice_key") or "").strip()
    if not speaker.startswith("S_"):   # 仅豆包/火山 S_ 音色可合成试听样音
        return d
    try:
        preview = generate_doubao_preview(speaker, PUBLIC_VOICE_SAMPLE_TEXT)
        fn = preview.get("file")
        if fn:
            url = public_url(fn, "audio/mpeg")   # 走 COS 直链（与其它产出一致）
            now = int(time.time())
            with closing(adb()) as c:
                c.execute("UPDATE audio_voices SET preview_file=?, preview_url=?, updated_at=? WHERE id=?",
                          (fn, url, now, d["id"]))
                c.commit()
            d["preview_file"] = fn
            d["preview_url"] = url
    except Exception as e:
        print("[list_audio_voices] 公共音色试听样音生成失败 voice=%s error=%s" %
              (d.get("voice_key"), str(e)[:200]), flush=True)
    return d

def list_audio_voices(username):
    with closing(adb()) as c:
        rows = c.execute("""SELECT id, scope, username, voice_key, display_name, provider_voice, preview_file, preview_url, slot_id, created_at, updated_at
            FROM audio_voices
            WHERE scope='public' OR (scope='personal' AND username=?)
            ORDER BY CASE scope WHEN 'public' THEN 0 ELSE 1 END, id""", (username,)).fetchall()
    # 公共音色缺试听样音时按需懒生成一次并缓存（前端只在有 preview_url 时才显示 ▶ 试听）
    return [_ensure_public_voice_preview(r) for r in rows]

def rename_audio_voice(username, slot_id, display_name):
    slot_id = (slot_id or "").strip()
    name = (display_name or "").strip()
    if not slot_id:
        raise Exception("缺少音色槽位")
    if not name:
        raise Exception("请输入音色名称")
    name = name[:40]
    now = int(time.time())
    with closing(adb()) as c:
        slot = c.execute("""SELECT voice_id FROM audio_voice_slots
            WHERE username=? AND slot_id=?""", (username, slot_id)).fetchone()
        if not slot or not slot["voice_id"]:
            raise Exception("音色不存在")
        cur = c.execute("""UPDATE audio_voices
            SET display_name=?, updated_at=?
            WHERE id=? AND username=? AND scope='personal'""",
            (name, now, slot["voice_id"], username))
        if cur.rowcount < 1:
            raise Exception("音色不存在")
        c.execute("UPDATE audio_voice_slots SET updated_at=? WHERE username=? AND slot_id=?",
                  (now, username, slot_id))
        c.commit()
    return {"slot_id": slot_id, "display_name": name, "updated_at": now}

def list_audio_assets(username, limit=120):
    limit = max(1, min(120, int(limit or 120)))
    with closing(adb()) as c:
        _ensure_column(c, "audio_assets", "deleted", "INTEGER DEFAULT 0")
        rows = c.execute("""SELECT a.id, a.job_id, a.username, a.voice_id, a.voice_key, a.file, a.url, a.text,
                   a.speed, a.pitch, a.volume, a.created_at, v.display_name AS voice_name, v.preview_url
            FROM audio_assets a
            LEFT JOIN audio_voices v ON v.id = a.voice_id
            WHERE a.username=? AND COALESCE(a.deleted,0)=0
            ORDER BY a.id DESC LIMIT ?""", (username, limit)).fetchall()
    return [dict(r) for r in rows]

# ============ 配音能力：OpenAI TTS（同事的 audio 能力，合并保留） ============
VOICE_MAP = {
    "personal": os.environ.get("VOICE_PERSONAL", "alloy"),
    "alloy": "alloy", "ash": "ash", "ballad": "ballad", "coral": "coral", "echo": "echo",
    "fable": "fable", "nova": "nova", "onyx": "onyx", "sage": "sage", "shimmer": "shimmer",
}
SPEED_MAP = {"slow": 0.88, "normal": 1.0, "fast": 1.12, "偏慢": 0.88, "正常": 1.0, "偏快": 1.12}

def gen_audio(payload):
    text = (payload.get("text") or payload.get("prompt") or "").strip()
    if not text:
        raise ValueError("配音文案不能为空")
    if len(text) > 1200:
        raise ValueError("配音文案过长，请控制在 1200 字以内")
    username = (payload.get("_username") or "").strip()
    raw_voice_key = (payload.get("voice") or "S_d21F8OR62").strip()
    voice_key = raw_voice_key.lower() if raw_voice_key.lower() in set() else raw_voice_key
    voice = resolve_audio_provider_voice(username, voice_key)
    raw_speed = payload.get("speed")
    if isinstance(raw_speed, (int, float)):
        speed = max(0.5, min(2.0, round(float(raw_speed), 1)))
    else:
        speed = SPEED_MAP.get(raw_speed or "normal", 1.0)
    def knob(name, minv, maxv, default):
        try:
            return max(minv, min(maxv, int(float(payload.get(name, default)))))
        except Exception:
            return default
    pitch = knob("pitch", -12, 12, 0)
    volume = knob("volume", -50, 100, 0)
    if str(voice).startswith("S_"):
        speech_rate = int(round((speed - 1.0) * 100))
        preview = generate_doubao_preview(voice, text, speech_rate=speech_rate, loudness_rate=volume, pitch_rate=pitch)
        fn = preview.get("file")
        return {"type": "audio", "file": fn, "url": preview.get("url"), "voice": voice_key,
                "speed": speed, "pitch": pitch, "volume": volume, "text": text, "prompt": text}
    instructions = "中文短视频口播配音，语气自然，吐字清晰，节奏适合美业/本地生活转化。"
    body = json.dumps({
        "model": TTS_MODEL, "voice": voice, "input": text,
        "instructions": instructions, "response_format": "mp3", "speed": speed,
    }, ensure_ascii=False).encode()
    data = _post_bytes("/v1/audio/speech", body, "application/json")
    fn = "audio/aud_%d.mp3" % int(time.time() * 1000)
    _out_path(fn).write_bytes(data)
    return {"type": "audio", "file": fn, "url": public_url(fn, "audio/mpeg"), "voice": voice_key,
            "speed": speed, "pitch": pitch, "volume": volume, "text": text, "prompt": text}

HANDLERS = {"audio": gen_audio}
