# -*- coding: utf-8 -*-
from .core import (
    DOUBAO_APPID, DOUBAO_CLONE_MODEL_TYPE, DOUBAO_CLONE_RESOURCE,
    DOUBAO_TOKEN, DOUBAO_TTS_RESOURCE, TTS_MODEL,
    _ensure_column, _file_url, _out_path, _post_bytes, _resolve_out_file,
    adb, base64, closing, jdb, json, os, public_url, re, subprocess,
    threading, time, urllib, uuid,
)
from .points import _auth_points_request
from . import cosyvoice, cos
from . import points as points_domain

VOICE_SLOT_COST = 50
VOICE_SLOT_MAX_PER_USER = 5
VALID_VOICE_SLOT_STATUSES = ("active", "training", "ready", "failed")
_voice_slot_purchase_lock = threading.Lock()


class VoiceSlotError(Exception):
    status = 500


class VoiceSlotLimitError(VoiceSlotError):
    status = 409


class VoiceSlotPurchaseError(VoiceSlotError):
    pass


class VoiceSlotNotFoundError(VoiceSlotError):
    status = 404


class VoiceSlotBusyError(VoiceSlotError):
    status = 409


class VoiceSlotEmptyError(VoiceSlotError):
    status = 400


def _valid_voice_slot_count(conn, username):
    placeholders = ",".join("?" for _ in VALID_VOICE_SLOT_STATUSES)
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM audio_voice_slots "
        "WHERE username=? AND status IN (%s)" % placeholders,
        (username,) + VALID_VOICE_SLOT_STATUSES,
    ).fetchone()
    return int(row["n"] if row else 0)


def count_user_audio_voice_slots(username):
    username = (username or "").strip()
    if not username:
        return 0
    with closing(adb()) as conn:
        return _valid_voice_slot_count(conn, username)


def purchase_audio_voice_slot(username):
    username = (username or "").strip()
    if not username:
        raise ValueError("missing username")

    with _voice_slot_purchase_lock:
        if count_user_audio_voice_slots(username) >= VOICE_SLOT_MAX_PER_USER:
            raise VoiceSlotLimitError("最多 %d 个音色槽位" % VOICE_SLOT_MAX_PER_USER)

        user_id = get_user_id(username)
        points_left = points_domain.deduct_points(username, VOICE_SLOT_COST, "voice_slot")
        slot_id = "slot_" + uuid.uuid4().hex
        now = int(time.time())
        try:
            with closing(adb()) as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    # Defensive recheck for an unexpected second writer process.
                    if _valid_voice_slot_count(conn, username) >= VOICE_SLOT_MAX_PER_USER:
                        raise VoiceSlotLimitError("最多 %d 个音色槽位" % VOICE_SLOT_MAX_PER_USER)
                    conn.execute("""INSERT INTO audio_voice_slots
                        (username, user_id, slot_id, status, created_at, updated_at)
                        VALUES(?,?,?,?,?,?)""",
                        (username, user_id, slot_id, "active", now, now))
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        except Exception as exc:
            points_domain.safe_refund_points(
                username, VOICE_SLOT_COST, "voice_slot:insert_failed")
            if isinstance(exc, VoiceSlotLimitError):
                raise
            raise VoiceSlotPurchaseError(
                "购买音色槽位失败，%d 点已退回" % VOICE_SLOT_COST) from exc

        return {
            "slot_id": slot_id,
            "status": "active",
            "cost": VOICE_SLOT_COST,
            "points_left": points_left,
        }

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
    fn = "audio/voice_preview_%s.mp3" % uuid.uuid4().hex  # 不可猜键(#185)
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
    # CosyVoice\uff1aprovider_voice \u662f CosyVoice voice_id \u5c31\u67e5\u5b83\u7684 list_voice \u72b6\u6001\uff0c\u4e0d\u78b0\u8c46\u5305\u3002
    if cosyvoice.enabled():
        with closing(adb()) as c:
            pv = c.execute("""SELECT provider_voice FROM audio_voices WHERE username=? AND slot_id=?
                ORDER BY id DESC LIMIT 1""", (username, slot_id)).fetchone()
        provider_voice = (pv["provider_voice"] if pv else "") or ""
        if provider_voice.startswith(cosyvoice.CLONE_MODEL):
            if slot["status"] == "failed":
                return {"status": "failed", "clone_error": slot["clone_error"] or "\u590d\u523b\u5931\u8d25"}
            try:
                cv_status, _ = cosyvoice.voice_status(provider_voice)
            except Exception:
                return {"status": slot["status"] or "training"}
            new_status = "ready" if cv_status == "OK" else ("failed" if cv_status not in ("", "OK") and "ing" not in cv_status.lower() else "training")
            if new_status != slot["status"]:
                with closing(adb()) as c:
                    c.execute("UPDATE audio_voice_slots SET status=?, updated_at=? WHERE username=? AND slot_id=?",
                              (new_status, int(time.time()), username, slot_id))
                    c.commit()
            return {"status": new_status, "cosy_status": cv_status}
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

CLONE_PREVIEW_TEXT = "你好，这是我的专属复刻音色试听。声音清晰自然，适合用于短视频口播和文案配音。"

CLONE_PREVIEW_TRIES = int(os.environ.get("CLONE_PREVIEW_TRIES", "5") or 5)

def _cosy_clone_preview(voice_id):
    """给刚复刻好的 CosyVoice 音色合成一句试听样音 → COS 直链。

    ⚠️ 复刻【当刻】音色模型常常还没就绪(#602)：create_voice / voice_status 返回 OK 之后，
    紧接着 synth 仍可能失败——音色还没进 list、或模型没即时加载。所以这里对 synth 做短重试
    轮询：**synth 本身能出声，才是「就绪」的权威信号**(比 voice_status 的 OK 更准)。
    失败返回 (None, None)，不阻断复刻(音色本身仍可用，只是暂无试听)。"""
    last = None
    for i in range(CLONE_PREVIEW_TRIES):
        try:
            data = cosyvoice.synth(voice_id, CLONE_PREVIEW_TEXT)
            if data and len(data) >= 1000:
                pf = "audio/voice_preview_%s.mp3" % uuid.uuid4().hex   # 不可猜键(#185)
                _out_path(pf).write_bytes(data)
                return pf, public_url(pf, "audio/mpeg")
            last = "音频过短(%d 字节)" % (len(data) if data else 0)
        except Exception as e:
            last = str(e)[:120]
        if i < CLONE_PREVIEW_TRIES - 1:
            time.sleep(2.0 + i)   # 2s,3s,4s,5s —— 给音色就绪留窗口
    print("[cosyvoice] 试听生成失败(%d 次) voice_id=%s: %s" % (CLONE_PREVIEW_TRIES, str(voice_id)[:22], last), flush=True)
    return None, None


def _cosy_backfill_preview_async(voice_id, username, voice_key):
    """复刻返回后【异步】生成试听并回填，不拖慢音色「就绪」。synth 对就绪窗口重试(#602)，
    成功就 UPDATE 该音色行的 preview(仅当其仍为空，避免覆盖已回填/后续重刻)。"""
    def _run():
        pf, url = _cosy_clone_preview(voice_id)
        if not url:
            return
        try:
            with closing(adb()) as c:
                c.execute("""UPDATE audio_voices SET preview_file=?, preview_url=?, updated_at=?
                    WHERE username=? AND scope='personal' AND voice_key=?
                      AND (preview_url IS NULL OR preview_url='')""",
                    (pf, url, int(time.time()), username, voice_key))
                c.commit()
        except Exception as e:
            print("[cosyvoice] 试听回填落库失败: %s" % str(e)[:120], flush=True)
    threading.Thread(target=_run, name="cosy-preview-backfill", daemon=True).start()

def _clone_via_cosyvoice(username, slot_id, name, audio_b64):
    """CosyVoice 复刻：60s 参考音频(已由 prepare_clone_audio 标准化) → COS 预签名 URL
    → create_voice 拿 voice_id → 落库。voice_id 直接作为 provider_voice，合成时按它选复刻模型。
    坑位免费，所以不再走豆包那套付费 slot 校验；create_voice 同步返回，可用即 ready。"""
    if not cos.enabled():
        raise ValueError("声音复刻需要 COS 存参考音频，当前未启用 COS")
    raw = base64.b64decode(audio_b64)
    key = "voice-clone-input/%s_%d.mp3" % (uuid.uuid4().hex, int(time.time()))
    tmp = _out_path("audio/_cvref_%d.mp3" % int(time.time() * 1000))
    tmp.write_bytes(raw)
    try:
        cos.upload(str(tmp), key)
        ref_url = cos.object_url(key, private=True)   # 短时效预签名，阿里同步拉取即可
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass
    voice_id = cosyvoice.create_voice(ref_url)
    status, _ = cosyvoice.voice_status(voice_id)
    slot_status = "ready" if status == "OK" else "training"
    now = int(time.time())
    voice_key = "vip_" + re.sub(r"[^a-zA-Z0-9_\-]", "_", slot_id)
    with closing(adb()) as c:
        c.execute("""INSERT OR IGNORE INTO audio_voices
            (username, scope, voice_key, display_name, provider_voice, slot_id, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (username, "personal", voice_key, name, voice_id, slot_id, now, now))
        c.execute("""UPDATE audio_voices SET display_name=?, provider_voice=?, slot_id=?, updated_at=?
            WHERE username=? AND scope='personal' AND voice_key=?""",
            (name, voice_id, slot_id, now, username, voice_key))
        r = c.execute("SELECT id FROM audio_voices WHERE username=? AND scope='personal' AND voice_key=?",
                      (username, voice_key)).fetchone()
        vid_row = r["id"] if r else None
        c.execute("""UPDATE audio_voice_slots SET voice_id=?, status=?, clone_started_at=?, clone_upload_at=?,
            clone_error=NULL, clone_upload_speaker_id=?, updated_at=? WHERE username=? AND slot_id=?""",
            (vid_row, slot_status, now, now, voice_id, now, username, slot_id))
        c.commit()
    # CosyVoice 的 create_voice 不返样音，复刻后自己合成一句试听存 COS——否则前端没 preview_url
    # 就不显示试听按钮。⚠️试听【异步】生成:音色/坑位已先落 ready(用户立即可用)，试听在后台线程
    # 对就绪窗口重试后回填(#602)——不再用 slot_status 门控、也不拖慢「就绪」。
    _cosy_backfill_preview_async(voice_id, username, voice_key)
    return {"voice_id": vid_row, "voice_key": voice_key, "display_name": name,
            "provider_voice": voice_id, "status": slot_status}

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
    if not cosyvoice.enabled() and (not DOUBAO_APPID or not DOUBAO_TOKEN):
        raise ValueError("\u8c46\u5305\u58f0\u97f3\u590d\u523b\u914d\u7f6e\u672a\u5b8c\u6210")
    with closing(adb()) as c:
        slot = c.execute("""SELECT id, slot_id, voice_id FROM audio_voice_slots
            WHERE username=? AND slot_id=? AND status IN ('active','training','failed','ready')""", (username, slot_id)).fetchone()
    if not slot:
        raise ValueError("\u97f3\u8272\u69fd\u4f4d\u4e0d\u5b58\u5728\u6216\u4e0d\u5c5e\u4e8e\u5f53\u524d\u8d26\u53f7")
    audio_b64, audio_format = prepare_clone_audio(audio_b64, audio_format)
    if cosyvoice.enabled():
        return _clone_via_cosyvoice(username, slot_id, name, audio_b64)
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
    # #604: \u5230\u8fd9\u8bf4\u660e\u8fd9\u4e2a voice_key \u6ca1\u6709\u5bf9\u5e94\u7684\u771f\u5b9e\u97f3\u8272\u884c\u3002**\u4e0d\u518d\u4e3a alloy \u5360\u4f4d\u81ea\u52a8\u5efa personal \u884c**
    # \u2014\u2014\u90a3\u6b63\u662f\u300c\u5220\u4e86\u53c8\u56de\u6765\u300d\u7684\u6839\u56e0:\u9057\u7559 key(dapeng/zelong/personal)\u914d\u4e00\u6b21\u97f3\uff0crecord_audio_asset
    # \u5c31\u51ed\u7a7a\u5efa\u4e00\u6761 alloy \u5360\u4f4d\u3002\u771f\u590d\u523b\u7531 _clone_via_cosyvoice \u76f4\u63a5\u5efa\u884c(\u4e0a\u9762 SELECT \u4f1a\u547d\u4e2d)\uff0c\u8d70\u4e0d\u5230\u8fd9\u3002
    # \u5408\u6210\u4e0d\u9700\u8981 audio_voices \u884c(resolve \u56de\u843d alloy \u5373\u53ef)\uff0c\u6240\u4ee5\u8fd4\u56de None\uff0c\u8ba9 voice_id \u7559\u7a7a\u3002
    return None

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
    voice_id = ensure_audio_voice(username, voice_key)  # #604: 遗留/占位 key 现在返 None，voice_id 存 NULL、不再凭空建占位行
    with closing(adb()) as c:
        c.execute("""INSERT OR REPLACE INTO audio_assets
            (job_id, username, voice_id, voice_key, file, url, text, speed, pitch, volume, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, username, voice_id, voice_key, result.get("file"), result.get("url"),
             result.get("text") or result.get("prompt"), result.get("speed"), result.get("pitch"),
             result.get("volume"), now))
        c.commit()

def _cleanup_alloy_placeholder_voices():
    """#604: 清掉个人 alloy 占位音色(幂等)。这些是遗留 key(dapeng/zelong/personal 等)被
    record_audio_asset→ensure_audio_voice 自动建出的空壳(provider_voice='alloy')——正是
    「删了又回来」的存量:每次启动 backfill_audio_assets 重放历史 audio job 就把它们重建回来。
    改了 ensure_audio_voice 不再自动建之后，这里把存量一次性删掉。真复刻(cosyvoice-*/豆包 S_)不动。"""
    try:
        with closing(adb()) as c:
            n = c.execute("DELETE FROM audio_voices WHERE scope='personal' AND provider_voice='alloy'").rowcount
            c.commit()
        if n:
            print("[audio] 清理 alloy 占位音色 %d 条(#604)" % n, flush=True)
    except Exception as e:
        print("[audio] alloy 占位清理失败: %s" % str(e)[:120], flush=True)

def backfill_audio_assets():
    _cleanup_alloy_placeholder_voices()   # #604: 先清存量占位，再重放(ensure 已不再重建)
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
_preview_warm_lock = threading.Lock()
_preview_warm_running = False
_preview_warm_next_at = 0

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
        print("[audio-preview-warmup] 公共音色试听样音生成失败 voice=%s error=%s" %
              (d.get("voice_key"), str(e)[:200]), flush=True)
    return d

def _warm_public_voice_previews(rows):
    global _preview_warm_running, _preview_warm_next_at
    try:
        for row in rows:
            _ensure_public_voice_preview(row)
    finally:
        with _preview_warm_lock:
            _preview_warm_running = False
            _preview_warm_next_at = time.time() + 300

def _schedule_public_preview_warmup(rows):
    """缺失试听样音时最多启动一个后台补齐线程；失败后冷却 5 分钟。"""
    global _preview_warm_running
    missing = [dict(r) for r in rows if r.get("scope") == "public" and not r.get("preview_url")]
    if not missing:
        return
    with _preview_warm_lock:
        if _preview_warm_running or time.time() < _preview_warm_next_at:
            return
        _preview_warm_running = True
    threading.Thread(target=_warm_public_voice_previews, args=(missing,),
                     name="audio-preview-warmup", daemon=True).start()

def list_audio_voices(username):
    with closing(adb()) as c:
        rows = c.execute("""SELECT id, scope, username, voice_key, display_name, provider_voice, preview_file, preview_url, slot_id, created_at, updated_at
            FROM audio_voices
            WHERE scope='public' OR (scope='personal' AND username=?)
            ORDER BY CASE scope WHEN 'public' THEN 0 ELSE 1 END, id""", (username,)).fetchall()
    items = [dict(r) for r in rows]
    _schedule_public_preview_warmup(items)
    return items

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


def delete_audio_voice(username, slot_id):
    """删除个人音色，但保留用户已经购买的槽位供重新克隆。"""
    username = (username or "").strip()
    slot_id = (slot_id or "").strip()
    if not slot_id:
        raise VoiceSlotEmptyError("缺少音色槽位")

    now = int(time.time())
    voice_id = None
    provider_voice = ""
    preview_file = ""
    preview_url = ""
    with closing(adb()) as c:
        try:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("""SELECT s.status, s.voice_id,
                       v.provider_voice, v.preview_file, v.preview_url
                FROM audio_voice_slots s
                LEFT JOIN audio_voices v
                  ON v.id=s.voice_id AND v.username=s.username AND v.scope='personal'
                WHERE s.username=? AND s.slot_id=?""", (username, slot_id)).fetchone()
            if not row:
                raise VoiceSlotNotFoundError("音色槽位不存在或不属于当前账号")
            if row["status"] == "training":
                raise VoiceSlotBusyError("音色正在克隆中，暂时不能删除")
            voice_id = row["voice_id"]
            if not voice_id:
                raise VoiceSlotEmptyError("当前槽位没有可删除的个人音色")

            provider_voice = (row["provider_voice"] or "").strip()
            preview_file = (row["preview_file"] or "").strip()
            preview_url = (row["preview_url"] or "").strip()
            c.execute("UPDATE audio_assets SET voice_id=NULL WHERE username=? AND voice_id=?",
                      (username, voice_id))
            c.execute("DELETE FROM audio_voices WHERE id=? AND username=? AND scope='personal'",
                      (voice_id, username))
            c.execute("""UPDATE audio_voice_slots
                SET status='active', voice_id=NULL, clone_started_at=NULL,
                    previous_preview_url=NULL, clone_upload_at=NULL, clone_error=NULL,
                    clone_upload_speaker_id=NULL, clone_upload_response=NULL,
                    clone_baseline_version=NULL, clone_baseline_icl_speaker_id=NULL,
                    clone_baseline_demo_audio=NULL, updated_at=?
                WHERE username=? AND slot_id=?""", (now, username, slot_id))
            c.commit()
        except Exception:
            c.rollback()
            raise

    # 数据库删除成功后再做外部清理，外部服务失败不影响用户继续使用已购槽位。
    refs = [preview_file]
    if preview_url.startswith("/api/gen/file/"):
        refs.append(preview_url[len("/api/gen/file/"):])
    for ref in refs:
        name = os.path.basename(str(ref or ""))
        if name.startswith("voice_preview_") and name.endswith(".mp3"):
            try:
                fp = _resolve_out_file(ref)
                if fp and fp.exists():
                    fp.unlink()
            except Exception as e:
                print("[delete_audio_voice] preview cleanup failed file=%s error=%s" %
                      (name, str(e)[:160]), flush=True)

    remote_deleted = False
    if provider_voice.startswith(cosyvoice.CLONE_MODEL) and cosyvoice.enabled():
        try:
            cosyvoice.delete_voice(provider_voice)
            remote_deleted = True
        except Exception as e:
            print("[delete_audio_voice] provider cleanup failed voice=%s error=%s" %
                  (provider_voice[:40], str(e)[:160]), flush=True)

    return {
        "slot_id": slot_id,
        "status": "active",
        "voice_deleted": True,
        "remote_deleted": remote_deleted,
    }

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

def _cosy_voice_for(provider_voice):
    """把库里的 provider_voice 翻成 CosyVoice 能用的 voice：
      * 4 个公共音色的豆包码(S_xxx) → 对应预置(longwan...)
      * 已是 CosyVoice 复刻 id → 原样
      * 其它(旧豆包个人音色/openai) → 尚未迁移，明确报错让用户重新复刻
    """
    v = str(provider_voice or "").strip()
    if v in cosyvoice.PUBLIC_VOICE_PRESETS:
        return cosyvoice.PUBLIC_VOICE_PRESETS[v]
    if v.startswith(cosyvoice.CLONE_MODEL):          # CosyVoice 复刻音色 id
        return v
    if v.startswith("S_") or v.startswith("vip_"):   # 旧豆包音色，还没迁到 CosyVoice
        raise ValueError("该音色尚未迁移到新引擎，请重新复刻一次")
    return v      # 兜底：当作预置名直接用

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

    # CosyVoice 全量通道：配了 DASHSCOPE_API_KEY 就走这里，否则回落豆包/OpenAI(合并零风险)。
    if cosyvoice.enabled():
        cv_voice = _cosy_voice_for(voice)
        # knob 的 pitch/-12~12、volume/-50~100 是豆包量纲；CosyVoice 用 pitch 0.5~2、volume 0~100。
        cv_audio = cosyvoice.synth(cv_voice, text, rate=speed,
                                   pitch=max(0.5, min(2.0, 1.0 + pitch / 24.0)),
                                   volume=max(0, min(100, 50 + volume // 2)))
        fn = "audio/aud_%d.mp3" % int(time.time() * 1000)   # 非敏感命名 → 可走 COS 公开直链
        _out_path(fn).write_bytes(cv_audio)
        return {"type": "audio", "file": fn, "url": public_url(fn, "audio/mpeg"), "voice": voice_key,
                "speed": speed, "pitch": pitch, "volume": volume, "text": text, "prompt": text}

    if str(voice).startswith("S_"):
        speech_rate = int(round((speed - 1.0) * 100))
        preview = generate_doubao_preview(voice, text, speech_rate=speech_rate, loudness_rate=volume, pitch_rate=pitch)
        fn = preview.get("file")
        # 正式配音不能复用 voice_preview 本地URL(敏感文件需归属鉴权且不走COS)。
        # 转存为独立 aud_*.mp3(非敏感)再 public_url→COS公开直链,可直接试听/下载。
        if fn:
            formal_fn = "audio/aud_%d.mp3" % int(time.time() * 1000)
            _out_path(formal_fn).write_bytes(_out_path(fn).read_bytes())
            try:
                _out_path(fn).unlink()
            except Exception:
                pass
            fn = formal_fn
        url = public_url(fn, "audio/mpeg") if fn else preview.get("url")
        return {"type": "audio", "file": fn, "url": url, "voice": voice_key,
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
