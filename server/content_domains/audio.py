# -*- coding: utf-8 -*-
import hashlib
import sqlite3

from .core import (
    TTS_MODEL,
    _ensure_column, _file_url, _out_path, _post_bytes, _resolve_out_file,
    adb, base64, closing, jdb, json, os, public_url, re, subprocess,
    threading, time, urllib, uuid,
)
from .points import _auth_points_request
from . import cosyvoice, cos
from . import points as points_domain
from . import pricing

VOICE_SLOT_COST = 50
VOICE_SLOT_MAX_PER_USER = 5
VALID_VOICE_SLOT_STATUSES = ("active", "training", "ready", "failed")
CLONE_TRAINING_STALE_SECONDS = 10 * 60
_voice_slot_purchase_lock = threading.Lock()
_clone_schema_lock = threading.Lock()
CLONE_ATTEMPT_LEASE_SECONDS = max(
    30, int(os.environ.get("CLONE_ATTEMPT_LEASE_SECONDS", "120") or 120),
)
CLONE_ATTEMPT_HEARTBEAT_SECONDS = max(
    1, min(30, int(os.environ.get("CLONE_ATTEMPT_HEARTBEAT_SECONDS", "10") or 10)),
)
_CLONE_ATTEMPT_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


class CloneAttemptError(ValueError):
    status = 409

    def __init__(self, detail, code="clone_attempt_mismatch"):
        super().__init__(detail)
        self.detail = detail
        self.code = code


def _ensure_clone_attempt_columns(connection):
    # Unit tests and mixed-version workers can race the first lazy migration.
    # Serialize the three ALTER TABLE checks so duplicate-column failures never
    # escape a legitimate clone request.
    with _clone_schema_lock:
        _ensure_column(connection, "audio_voice_slots", "clone_attempt_id", "TEXT")
        _ensure_column(connection, "audio_voice_slots", "clone_attempt_phase", "TEXT")
        _ensure_column(connection, "audio_voice_slots", "clone_attempt_updated_at", "INTEGER")


def _clean_clone_attempt_id(value, required=False):
    attempt_id = str(value or "").strip()
    if not attempt_id:
        if required:
            raise CloneAttemptError("声音复刻缺少本次操作标识", "clone_attempt_required")
        return ""
    if not _CLONE_ATTEMPT_RE.fullmatch(attempt_id):
        raise CloneAttemptError("声音复刻操作标识格式无效", "clone_attempt_invalid")
    return attempt_id

# Only server-authored delivery profiles may reach CosyVoice instruction
# control.  Keep these concise: CosyVoice counts CJK characters as two and
# limits an instruction to 100 characters.
DELIVERY_INSTRUCTIONS = {
    "natural": "",
    "energetic_hook": "请用有感染力的广告开场语气，情绪开心自然，重读核心词，短句间自然停顿，避免喊叫。",
    "clear_explain": "请用自然清晰的讲解语气，语速平稳，句间适度停顿，重点词轻微强调，避免机械播报。",
    "confident_cta": "请用亲切、自信、有行动感的收尾语气，结尾有力度，保持自然停顿，避免夸张。",
}


def normalize_audio_delivery(value):
    delivery = str(value or "natural").strip().lower()
    if delivery not in DELIVERY_INSTRUCTIONS:
        raise ValueError("delivery 参数无效")
    return delivery


def voice_slot_cost():
    return pricing.get_price("audio.voice_slot")


class VoiceSlotError(Exception):
    status = 500


class VoiceSlotLimitError(VoiceSlotError):
    status = 409


class VoiceSlotPurchaseError(VoiceSlotError):
    pass


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


def _membership_voice_slot_entitlement(username):
    q = urllib.parse.quote(str(username or ""), safe="")
    res = _auth_points_request(
        "/api/auth/membership/voice-slot-entitlement?username=" + q,
        method="GET",
    )
    return bool((res.get("entitlement") or {}).get("eligible"))


def ensure_membership_voice_slot(username):
    """幂等落地会员免费槽位；失败不扣点，下次读取或购买时自动重试。"""
    username = (username or "").strip()
    if not username or not _membership_voice_slot_entitlement(username):
        return None
    user_id = get_user_id(username)
    slot_id = "member_" + hashlib.sha256(username.encode("utf-8")).hexdigest()[:24]
    now = int(time.time())
    with _voice_slot_purchase_lock:
        with closing(adb()) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                if _valid_voice_slot_count(conn, username) > 0:
                    conn.commit()
                    return {"created": False, "slot_id": None, "status": "existing", "cost": 0}
                conn.execute(
                    """INSERT OR IGNORE INTO audio_voice_slots(
                           username,user_id,slot_id,status,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?)""",
                    (username, user_id, slot_id, "active", now, now),
                )
                created = bool(conn.execute("SELECT changes()").fetchone()[0])
                conn.commit()
                return {
                    "created": created,
                    "slot_id": slot_id,
                    "status": "active",
                    "cost": 0,
                }
            except Exception:
                conn.rollback()
                raise


def purchase_audio_voice_slot(username):
    username = (username or "").strip()
    if not username:
        raise ValueError("missing username")

    free_slot = ensure_membership_voice_slot(username)
    if free_slot and free_slot.get("created"):
        free_slot["points_left"] = None
        return free_slot

    with _voice_slot_purchase_lock:
        if count_user_audio_voice_slots(username) >= VOICE_SLOT_MAX_PER_USER:
            raise VoiceSlotLimitError("最多 %d 个音色槽位" % VOICE_SLOT_MAX_PER_USER)

        user_id = get_user_id(username)
        cost = voice_slot_cost()
        points_left = points_domain.deduct_points(username, cost, "voice_slot")
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
                username, cost, "voice_slot:insert_failed")
            if isinstance(exc, VoiceSlotLimitError):
                raise
            raise VoiceSlotPurchaseError(
                "购买音色槽位失败，%d 点已退回" % cost) from exc

        return {
            "slot_id": slot_id,
            "status": "active",
            "cost": cost,
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
    try:
        ensure_membership_voice_slot(username)
    except Exception:
        # 权益仍保留在 auth 数据库，下一次读取会继续尝试；不影响已有槽位展示。
        pass
    with closing(adb()) as c:
        rows = c.execute("""SELECT s.id, s.username, s.user_id, s.slot_id, s.status, s.voice_id, COALESCE(s.reclone_count, 0) AS reclone_count,
                   s.created_at, s.updated_at, s.clone_started_at, s.clone_upload_at, s.clone_error,
                   s.clone_upload_speaker_id, s.clone_upload_response,
                   v.display_name AS voice_name, v.provider_voice, v.voice_key,
                   v.preview_file, v.preview_url, v.updated_at AS voice_updated_at
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

def check_clone_status(username, slot_id, attempt_id=None):
    """Return only the state owned by ``attempt_id`` when one is supplied."""
    username = (username or "").strip()
    slot_id = (slot_id or "").strip()
    expected_attempt_id = _clean_clone_attempt_id(attempt_id)
    with closing(adb()) as c:
        _ensure_clone_attempt_columns(c)
        c.commit()
        slot = c.execute("""SELECT id, slot_id, status, voice_id, clone_started_at, clone_upload_at, clone_error,
                   clone_baseline_version, clone_baseline_icl_speaker_id, clone_baseline_demo_audio,
                   clone_attempt_id, clone_attempt_phase, clone_attempt_updated_at,
                   clone_upload_response, updated_at
            FROM audio_voice_slots
            WHERE username=? AND slot_id=?""", (username, slot_id)).fetchone()
        voice = c.execute("""SELECT display_name, provider_voice, preview_url FROM audio_voices
            WHERE id=? AND username=? AND slot_id=?""",
            (slot["voice_id"] if slot else -1, username, slot_id)).fetchone()
    if not slot:
        raise ValueError("\u97f3\u8272\u69fd\u4f4d\u4e0d\u5b58\u5728\u6216\u4e0d\u5c5e\u4e8e\u5f53\u524d\u8d26\u53f7")
    current_attempt_id = str(slot["clone_attempt_id"] or "")
    if expected_attempt_id and current_attempt_id != expected_attempt_id:
        raise CloneAttemptError("本次声音复刻操作已被更新，请重新附加样音后重试")

    def response(status, **extra):
        result = {"status": status}
        if current_attempt_id:
            result["attempt_id"] = current_attempt_id
        result.update({key: value for key, value in extra.items() if value not in (None, "")})
        return result

    if (slot["status"] == "training" and slot["voice_id"] and voice
            and str(voice["provider_voice"] or "").startswith(cosyvoice.CLONE_MODEL)
            and voice["preview_url"]):
        now = int(time.time())
        with closing(adb()) as c:
            _ensure_clone_attempt_columns(c)
            cur = c.execute("""UPDATE audio_voice_slots SET status='ready', clone_attempt_phase='ready',
                    clone_attempt_updated_at=?, updated_at=?
                WHERE id=? AND username=? AND slot_id=? AND status='training' AND voice_id=?
                  AND COALESCE(clone_attempt_id,'')=?
                  AND EXISTS (
                    SELECT 1 FROM audio_voices v
                    WHERE v.id=audio_voice_slots.voice_id AND v.scope='personal'
                      AND v.username=? AND v.slot_id=? AND v.provider_voice=?
                      AND v.preview_url=?
                  )""", (now, now, slot["id"], username, slot_id, slot["voice_id"],
                           current_attempt_id, username, slot_id,
                           voice["provider_voice"], voice["preview_url"]))
            c.commit()
        if cur.rowcount == 1:
            return response("ready", preview_url=voice["preview_url"])
        with closing(adb()) as c:
            _ensure_clone_attempt_columns(c)
            current = c.execute("""SELECT s.status, s.clone_error, s.clone_attempt_id, v.preview_url
                FROM audio_voice_slots s LEFT JOIN audio_voices v ON v.id=s.voice_id
                WHERE s.username=? AND s.slot_id=?""", (username, slot_id)).fetchone()
        if expected_attempt_id and (not current or str(current["clone_attempt_id"] or "") != expected_attempt_id):
            raise CloneAttemptError("本次声音复刻操作已被更新，请重新附加样音后重试")
        result = response((current["status"] if current else "training") or "training")
        if current and current["preview_url"]:
            result["preview_url"] = current["preview_url"]
        if current and current["clone_error"]:
            result["clone_error"] = current["clone_error"]
        return result
    if slot["status"] == "failed":
        return response(
            "failed",
            clone_error=slot["clone_error"] or "声音复刻失败，请重新上传清晰样音",
        )
    if not cosyvoice.enabled():
        return response("failed", clone_error="声音复刻服务暂不可用")
    # Until the provider voice id replaces the slot placeholder, this attempt
    # is still training and no provider status call is safe.
    provider_voice = (voice["provider_voice"] if voice else "") or ""
    if provider_voice.startswith(cosyvoice.CLONE_MODEL):
        if slot["status"] == "failed":
            return response("failed", clone_error=slot["clone_error"] or "\u590d\u523b\u5931\u8d25")
        try:
            cv_status, _ = cosyvoice.voice_status(provider_voice)
        except Exception:
            return response(slot["status"] or "training")
        new_status = "ready" if cv_status == "OK" else ("failed" if cv_status not in ("", "OK") and "ing" not in cv_status.lower() else "training")
        if new_status != slot["status"]:
            with closing(adb()) as c:
                _ensure_clone_attempt_columns(c)
                now = int(time.time())
                cur = c.execute("""UPDATE audio_voice_slots SET status=?, clone_attempt_phase=?,
                              clone_attempt_updated_at=?, updated_at=?
                           WHERE username=? AND slot_id=? AND COALESCE(clone_attempt_id,'')=?
                             AND COALESCE(clone_upload_response,'')=?""",
                          (new_status, "ready" if new_status == "ready" else
                           ("failed" if new_status == "failed" else "provider_training"),
                           now, now, username, slot_id, current_attempt_id,
                           str(slot["clone_upload_response"] or "")))
                c.commit()
            if cur.rowcount != 1:
                with closing(adb()) as c:
                    current = c.execute("""SELECT status,clone_error
                        FROM audio_voice_slots WHERE username=? AND slot_id=?""",
                        (username, slot_id)).fetchone()
                return response(
                    (current["status"] if current else "training") or "training",
                    clone_error=(current["clone_error"] if current else ""),
                )
        return response(new_status, cosy_status=cv_status)
    if slot["status"] == "training":
        last_at = max(
            int(slot["clone_started_at"] or 0),
            int(slot["clone_upload_at"] or 0),
            int(slot["updated_at"] or 0),
        )
        if (not current_attempt_id and last_at
                and int(time.time()) - last_at >= CLONE_TRAINING_STALE_SECONDS):
            with closing(adb()) as c:
                now = int(time.time())
                cur = c.execute("""UPDATE audio_voice_slots
                    SET status='failed', clone_error=?, updated_at=?
                    WHERE username=? AND slot_id=? AND status='training'
                      AND COALESCE(clone_attempt_id,'')=''
                      AND COALESCE(clone_upload_response,'')=?""",
                    ("声音复刻任务已中断，请重新上传样音", now,
                     username, slot_id, str(slot["clone_upload_response"] or "")))
                c.commit()
            if cur.rowcount == 1:
                return response(
                    "failed", clone_error="声音复刻任务已中断，请重新上传样音",
                )
        return response("training")
    return response("failed", clone_error="该音色来自已停用渠道，请重新复刻")

ALLOWED_CLONE_AUDIO_FORMATS = {"mp3", "wav", "m4a", "aac", "ogg"}

class CloneVipValidationError(ValueError):
    def __init__(self, status, detail, code="voice_clone_invalid"):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.code = code

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
    checked = dict(payload)
    checked["slot_id"] = slot_id
    checked["audio"] = audio_b64
    checked["audio_format"] = audio_format
    return checked


def clone_request_replay(username, slot_id, request_id, request_digest=""):
    """Compatibility replay for the Tang CLI voice-clone endpoint."""
    if not request_id:
        return None
    with closing(adb()) as c:
        _ensure_column(c, "audio_voice_slots", "clone_upload_response", "TEXT")
        c.commit()
        rows = c.execute("""SELECT s.slot_id,s.status,s.clone_error,s.clone_upload_response,
                    v.voice_key,v.display_name
                FROM audio_voice_slots s
                LEFT JOIN audio_voices v ON v.id=s.voice_id
                WHERE s.username=? AND s.clone_upload_response IS NOT NULL""",
                (username,)).fetchall()
    for row in rows:
        try:
            meta = json.loads(row["clone_upload_response"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if meta.get("idempotency_key") != request_id:
            continue
        if (row["slot_id"] != slot_id
                or (request_digest and meta.get("request_digest") != request_digest)):
            raise CloneVipValidationError(
                409, "同一个幂等键不能用于不同的声音样音", "idempotency_conflict",
            )
        return {
            "voice_key": row["voice_key"], "display_name": row["display_name"],
            "preview_url": None, "status": row["status"],
            "clone_error": row["clone_error"] or "", "replayed": True,
        }
    return None

def clone_attempt_snapshot(username, slot_id, attempt_id, now=None):
    """Inspect a processing claim without trusting another slot operation."""
    attempt_id = _clean_clone_attempt_id(attempt_id, required=True)
    with closing(adb()) as c:
        _ensure_clone_attempt_columns(c)
        c.commit()
        row = c.execute("""SELECT s.status, s.voice_id, s.clone_error,
                    s.clone_attempt_id, s.clone_attempt_phase,
                    s.clone_attempt_updated_at, s.clone_upload_speaker_id,
                    COALESCE(s.reclone_count, 0) AS reclone_count,
                    s.updated_at, v.username AS voice_username,
                    v.scope AS voice_scope, v.voice_key, v.provider_voice,
                    v.slot_id AS voice_slot_id, v.updated_at AS voice_updated_at
                FROM audio_voice_slots s
                LEFT JOIN audio_voices v ON v.id=s.voice_id
                WHERE s.username=? AND s.slot_id=?""",
                ((username or "").strip(), (slot_id or "").strip())).fetchone()
    if not row or str(row["clone_attempt_id"] or "") != attempt_id:
        return {"action": "mismatch"}
    phase = str(row["clone_attempt_phase"] or "")
    expected_voice_key = "vip_" + re.sub(
        r"[^a-zA-Z0-9_\-]", "_", (slot_id or "").strip()
    )
    provider_voice = str(row["provider_voice"] or "")
    ready_version = (
        row["status"] == "ready" and phase == "ready"
        and bool(row["voice_id"]) and bool(provider_voice)
        and str(row["clone_upload_speaker_id"] or "") == provider_voice
        and str(row["voice_username"] or "") == (username or "").strip()
        and str(row["voice_scope"] or "") == "personal"
        and str(row["voice_key"] or "") == expected_voice_key
        and str(row["voice_slot_id"] or "") == (slot_id or "").strip()
    )
    if ready_version:
        return {
            "action": "ready", "attempt_id": attempt_id,
            "voice_id": row["voice_id"], "provider_voice": provider_voice,
            "reclone_count": int(row["reclone_count"] or 0),
            "updated_at": int(row["updated_at"] or 0),
            "voice_updated_at": int(row["voice_updated_at"] or 0),
        }
    if phase == "failed" or row["status"] == "failed":
        return {"action": "failed", "attempt_id": attempt_id,
                "clone_error": str(row["clone_error"] or "声音复刻失败")}
    if phase == "provider_training":
        # The provider has already accepted this operation and its persisted
        # voice id is authoritative. The local worker lease no longer applies:
        # even a long provider training window must never start it again.
        return {"action": "provider_training", "attempt_id": attempt_id,
                "voice_id": row["voice_id"]}
    updated_at = int(row["clone_attempt_updated_at"] or 0)
    age = max(0, int(time.time() if now is None else now) - updated_at)
    return {"action": "stale" if not updated_at or age > CLONE_ATTEMPT_LEASE_SECONDS else "processing",
            "attempt_id": attempt_id, "age": age}


def _update_clone_attempt(username, slot_id, attempt_id, phase, status=None, error=None):
    attempt_id = _clean_clone_attempt_id(attempt_id, required=True)
    now = int(time.time())
    with closing(adb()) as c:
        _ensure_clone_attempt_columns(c)
        assignments = ["clone_attempt_phase=?", "clone_attempt_updated_at=?", "updated_at=?"]
        values = [phase, now, now]
        if status:
            assignments.append("status=?")
            values.append(status)
        if error is not None:
            assignments.append("clone_error=?")
            values.append(error)
        values.extend([(username or "").strip(), (slot_id or "").strip(), attempt_id])
        cur = c.execute("UPDATE audio_voice_slots SET %s WHERE username=? AND slot_id=? AND clone_attempt_id=?" %
                        ",".join(assignments), tuple(values))
        c.commit()
        return cur.rowcount == 1


def mark_clone_attempt_running(username, slot_id, attempt_id):
    return _update_clone_attempt(username, slot_id, attempt_id, "running", status="training")


def fail_clone_attempt(username, slot_id, attempt_id, error):
    return _update_clone_attempt(username, slot_id, attempt_id, "failed", status="failed", error=str(error or "声音复刻失败")[:220])


def mark_clone_training(
        username, slot_id, name, attempt_id=None, request_digest="",
        expected_preimage=None):
    username = (username or "").strip()
    slot_id = (slot_id or "").strip()
    name = (name or "\u6211\u7684VIP\u590d\u523b\u97f3\u8272").strip()[:40]
    attempt_id = _clean_clone_attempt_id(attempt_id) or ("legacy-" + uuid.uuid4().hex)
    now = int(time.time())
    voice_key = "vip_" + re.sub(r"[^a-zA-Z0-9_\\-]", "_", slot_id)
    with closing(adb()) as c:
        _ensure_clone_attempt_columns(c)
        _ensure_column(c, "audio_voice_slots", "clone_upload_response", "TEXT")
        c.commit()
        c.execute("BEGIN IMMEDIATE")
        if request_digest:
            replay_rows = c.execute("""SELECT s.slot_id,s.status,s.voice_id,
                        s.clone_error,s.clone_upload_response,
                        COALESCE(s.reclone_count,0) AS reclone_count,
                        v.voice_key,v.display_name
                    FROM audio_voice_slots s
                    LEFT JOIN audio_voices v ON v.id=s.voice_id
                    WHERE s.username=? AND s.clone_upload_response IS NOT NULL""",
                    (username,)).fetchall()
            for replay_row in replay_rows:
                try:
                    meta = json.loads(replay_row["clone_upload_response"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if meta.get("idempotency_key") != attempt_id:
                    continue
                if (replay_row["slot_id"] != slot_id
                        or meta.get("request_digest") != request_digest):
                    c.rollback()
                    raise CloneVipValidationError(
                        409, "同一个幂等键不能用于不同的声音样音",
                        "idempotency_conflict",
                    )
                c.commit()
                return {
                    "voice_id": replay_row["voice_id"],
                    "voice_key": replay_row["voice_key"],
                    "display_name": replay_row["display_name"] or name,
                    "status": replay_row["status"],
                    "reclone_count": int(replay_row["reclone_count"] or 0),
                    "attempt_id": attempt_id,
                    "replayed": True,
                }
        slot = c.execute("""SELECT id, status, voice_id, COALESCE(reclone_count, 0) AS reclone_count,
                    updated_at, clone_upload_at, clone_attempt_id,
                    clone_upload_response FROM audio_voice_slots
            WHERE username=? AND slot_id=?""",
            (username, slot_id)).fetchone()
        if not slot:
            raise ValueError("\u97f3\u8272\u69fd\u4f4d\u4e0d\u5b58\u5728\u6216\u4e0d\u5c5e\u4e8e\u5f53\u524d\u8d26\u53f7")
        if slot["status"] == "training":
            if str(slot["clone_attempt_id"] or "") == attempt_id:
                existing_meta = {}
                try:
                    existing_meta = json.loads(slot["clone_upload_response"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    existing_meta = {}
                if (request_digest and existing_meta.get("request_digest")
                        and existing_meta.get("request_digest") != request_digest):
                    c.rollback()
                    raise CloneVipValidationError(
                        409, "同一个幂等键不能用于不同的声音样音",
                        "idempotency_conflict",
                    )
                c.commit()
                return {"voice_id": slot["voice_id"], "voice_key": voice_key,
                        "display_name": name, "status": "training",
                        "reclone_count": int(slot["reclone_count"] or 0),
                        "attempt_id": attempt_id,
                        **({"replayed": True} if request_digest else {})}
            last_at = int(slot["clone_upload_at"] or slot["updated_at"] or 0)
            if last_at and now - last_at < 600:
                raise ValueError("\u97f3\u8272\u6b63\u5728\u590d\u523b\u4e2d\uff0c\u8bf7\u7b49\u5f85\u5b8c\u6210")
        is_reclone = slot["status"] == "ready" and bool(slot["voice_id"])
        reclone_count = int(slot["reclone_count"] or 0)
        next_reclone_count = reclone_count + 1 if is_reclone else reclone_count
        c.execute("""INSERT OR IGNORE INTO audio_voices
            (username, scope, voice_key, display_name, provider_voice, slot_id, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (username, "personal", voice_key, name, slot_id, slot_id, now, now))
        r = c.execute("SELECT id FROM audio_voices WHERE username=? AND scope='personal' AND voice_key=?",
                      (username, voice_key)).fetchone()
        voice_id = r["id"] if r else None
        where = "username=? AND slot_id=?"
        where_values = [username, slot_id]
        if expected_preimage is not None:
            expected = dict(expected_preimage or {})
            required = {
                "status", "voice_name", "voice_id", "provider_voice", "reclone_count",
                "updated_at", "voice_updated_at", "version",
            }
            if not required.issubset(expected):
                c.rollback()
                raise CloneAttemptError(
                    "音色槽位授权版本不完整，请重新授权",
                    "clone_slot_preimage_invalid",
                )
            where += """ AND status=? AND COALESCE(voice_id,0)=?
                AND COALESCE(reclone_count,0)=? AND COALESCE(updated_at,0)=?
                AND ((?=0 AND voice_id IS NULL AND ?='' AND ?=0)
                  OR (?<>0 AND EXISTS(
                    SELECT 1 FROM audio_voices original
                    WHERE original.id=audio_voice_slots.voice_id
                      AND original.username=? AND original.scope='personal'
                      AND COALESCE(original.display_name,'')=?
                      AND COALESCE(original.provider_voice,'')=?
                      AND COALESCE(original.updated_at,0)=?
                  )))"""
            expected_voice_id = int(expected.get("voice_id") or 0)
            expected_provider = str(expected.get("provider_voice") or "")
            expected_voice_updated = int(expected.get("voice_updated_at") or 0)
            where_values.extend([
                str(expected.get("status") or ""), expected_voice_id,
                int(expected.get("reclone_count") or 0),
                int(expected.get("updated_at") or 0), expected_voice_id,
                expected_provider, expected_voice_updated, expected_voice_id,
                username, str(expected.get("voice_name") or ""),
                expected_provider, expected_voice_updated,
            ])
        request_meta = (json.dumps({
            "idempotency_key": attempt_id,
            "request_digest": str(request_digest or ""),
        }, ensure_ascii=False, sort_keys=True) if request_digest else "")
        cur = c.execute("""UPDATE audio_voice_slots SET voice_id=?, status='training', reclone_count=?, clone_started_at=?,
                clone_upload_at=NULL, clone_error=NULL, clone_attempt_id=?, clone_attempt_phase='accepted',
                clone_attempt_updated_at=?, clone_upload_response=?, updated_at=? WHERE """ + where,
                tuple([voice_id, next_reclone_count, now, attempt_id, now,
                       request_meta, now] + where_values))
        if cur.rowcount != 1:
            c.rollback()
            raise CloneAttemptError(
                "音色槽位在授权后发生变化，请重新选择并确认",
                "clone_slot_preimage_mismatch",
            )
        c.execute("""UPDATE audio_voices
            SET display_name=?, provider_voice=?, slot_id=?, updated_at=?
            WHERE username=? AND scope='personal' AND voice_key=?""",
            (name, slot_id, slot_id, now, username, voice_key))
        c.commit()
    clear_voice_preview(username, slot_id)
    return {"voice_id": voice_id, "voice_key": voice_key, "display_name": name,
            "status": "training", "reclone_count": next_reclone_count,
            "attempt_id": attempt_id}

def clone_vip_voice_background(username, payload):
    attempt_id = _clean_clone_attempt_id(
        payload.get("clone_attempt_id") or payload.get("_request_id"),
        required=True,
    )
    slot_id = (payload.get("slot_id") or "").strip()
    stop = threading.Event()
    def heartbeat():
        while not stop.wait(CLONE_ATTEMPT_HEARTBEAT_SECONDS):
            if not _update_clone_attempt(username, slot_id, attempt_id, "running"):
                return
    heart = threading.Thread(target=heartbeat, name="clone-attempt-heartbeat", daemon=True)
    heart.start()
    try:
        clone_vip_voice(username, payload)
    except Exception as e:
        print("[clone_vip_voice_background] failed username=%s slot_id=%s error=%s" % (username, slot_id, str(e)[:300]), flush=True)
        if slot_id:
            try:
                fail_clone_attempt(username, slot_id, attempt_id, _clone_error_message(e))
            except Exception:
                pass
    finally:
        stop.set()


CLONE_COS_UPLOAD_ATTEMPTS = 3


def _is_transient_clone_upload_error(error):
    """Only retry connection/TLS failures; validation and provider errors fail fast."""
    message = ("%s %s" % (error.__class__.__name__, str(error))).lower()
    return any(token in message for token in (
        "ssl", "tls", "eof", "timed out", "timeout", "connection reset",
        "connection aborted", "connectionerror", "temporarily unavailable",
    ))


def _upload_clone_reference(local_path, key):
    for attempt in range(CLONE_COS_UPLOAD_ATTEMPTS):
        try:
            return cos.upload(local_path, key)
        except Exception as error:
            if attempt + 1 >= CLONE_COS_UPLOAD_ATTEMPTS or not _is_transient_clone_upload_error(error):
                raise
            time.sleep(1 << attempt)


def _clone_error_message(error):
    message = str(error or "")
    if "Audio.AudioShortError" in message or "valid audio too short" in message.lower():
        return "样音中的有效语音太短，请上传30至60秒连续、清晰、单人说话的样音"
    if _is_transient_clone_upload_error(error):
        return "样音上传网络异常，系统重试后仍未成功，请稍后重试"
    return "声音复刻失败，请检查样音后重试"

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


def _cosy_backfill_preview_async(voice_id, username, voice_key, attempt_id=""):
    """复刻返回后【异步】生成试听并回填，不拖慢音色「就绪」。synth 对就绪窗口重试(#602)，
    成功就 UPDATE 该音色行的 preview 并同步槽位 ready。provider_voice 必须仍匹配本次
    复刻，避免旧异步任务覆盖随后发起的新复刻。"""
    def _run():
        pf, url = _cosy_clone_preview(voice_id)
        if not url:
            return
        try:
            with closing(adb()) as c:
                now = int(time.time())
                cur = c.execute("""UPDATE audio_voices SET preview_file=?, preview_url=?, updated_at=?
                    WHERE username=? AND scope='personal' AND voice_key=?
                      AND provider_voice=?
                      AND (preview_url IS NULL OR preview_url='')""",
                    (pf, url, now, username, voice_key, voice_id))
                if cur.rowcount:
                    _ensure_clone_attempt_columns(c)
                    c.execute("""UPDATE audio_voice_slots SET status='ready', clone_attempt_phase='ready',
                        clone_attempt_updated_at=?, updated_at=?
                        WHERE username=? AND status='training' AND voice_id IN (
                            SELECT id FROM audio_voices
                            WHERE username=? AND scope='personal' AND voice_key=?
                              AND provider_voice=?
                        ) AND (?='' OR clone_attempt_id=?)""",
                        (now, now, username, username, voice_key, voice_id,
                         attempt_id, attempt_id))
                c.commit()
        except Exception as e:
            print("[cosyvoice] 试听回填落库失败: %s" % str(e)[:120], flush=True)
    threading.Thread(target=_run, name="cosy-preview-backfill", daemon=True).start()

def _clone_via_cosyvoice(username, slot_id, name, audio_b64, attempt_id):
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
        _upload_clone_reference(str(tmp), key)
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
        _ensure_clone_attempt_columns(c)
        c.execute("BEGIN IMMEDIATE")
        owner = c.execute("""SELECT clone_attempt_id FROM audio_voice_slots
            WHERE username=? AND slot_id=?""", (username, slot_id)).fetchone()
        if not owner or str(owner["clone_attempt_id"] or "") != attempt_id:
            c.rollback()
            return {"stale": True, "status": "superseded",
                    "attempt_id": attempt_id}
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
        cur = c.execute("""UPDATE audio_voice_slots SET voice_id=?, status=?, clone_started_at=?, clone_upload_at=?,
            clone_error=NULL, clone_upload_speaker_id=?, clone_attempt_phase=?, clone_attempt_updated_at=?,
            updated_at=? WHERE username=? AND slot_id=? AND clone_attempt_id=?""",
            (vid_row, slot_status, now, now, voice_id,
             "ready" if slot_status == "ready" else "provider_training",
             now, now, username, slot_id, attempt_id))
        if cur.rowcount != 1:
            c.rollback()
            return {"stale": True, "status": "superseded",
                    "attempt_id": attempt_id}
        c.commit()
    # CosyVoice 的 create_voice 不返样音，复刻后自己合成一句试听存 COS——否则前端没 preview_url
    # 就不显示试听按钮。⚠️试听【异步】生成:音色/坑位已先落 ready(用户立即可用)，试听在后台线程
    # 对就绪窗口重试后回填(#602)——不再用 slot_status 门控、也不拖慢「就绪」。
    _cosy_backfill_preview_async(voice_id, username, voice_key, attempt_id)
    return {"voice_id": vid_row, "voice_key": voice_key, "display_name": name,
            "provider_voice": voice_id, "status": slot_status,
            "attempt_id": attempt_id}

def clone_vip_voice(username, payload):
    username = (username or "").strip()
    slot_id = (payload.get("slot_id") or "").strip()
    name = (payload.get("name") or "\u6211\u7684VIP\u590d\u523b\u97f3\u8272").strip()[:40]
    audio_b64 = _clone_audio_b64(payload.get("audio"))
    audio_format = _clone_audio_format(payload.get("audio_format"))
    attempt_id = _clean_clone_attempt_id(
        payload.get("clone_attempt_id") or payload.get("_request_id"),
        required=True,
    )
    if audio_format not in ALLOWED_CLONE_AUDIO_FORMATS:
        raise ValueError("audio_format 仅支持 mp3/wav/m4a/aac/ogg")
    if not slot_id:
        raise ValueError("\u7f3a\u5c11\u97f3\u8272\u69fd\u4f4d")
    if not audio_b64:
        raise ValueError("\u8bf7\u5148\u4e0a\u4f20\u6837\u97f3")
    if not cosyvoice.enabled():
        raise ValueError("声音复刻服务暂不可用")
    with closing(adb()) as c:
        _ensure_clone_attempt_columns(c)
        c.commit()
        slot = c.execute("""SELECT id, slot_id, voice_id FROM audio_voice_slots
            WHERE username=? AND slot_id=? AND clone_attempt_id=?
              AND status IN ('active','training','failed','ready')""",
            (username, slot_id, attempt_id)).fetchone()
    if not slot:
        raise ValueError("\u97f3\u8272\u69fd\u4f4d\u4e0d\u5b58\u5728\u6216\u4e0d\u5c5e\u4e8e\u5f53\u524d\u8d26\u53f7")
    audio_b64, audio_format = prepare_clone_audio(audio_b64, audio_format)
    return _clone_via_cosyvoice(username, slot_id, name, audio_b64, attempt_id)

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
    asset_kind = str(result.get("asset_kind") or "voice")
    if asset_kind == "sound_effect":
        voice_key, voice_id = "", None
    else:
        raw_voice_key = (result.get("voice") or "S_d21F8OR62").strip()
        voice_key = raw_voice_key.lower() if raw_voice_key.lower() in set() else raw_voice_key
        voice_id = ensure_audio_voice(username, voice_key)
    with closing(adb()) as c:
        _ensure_column(c, "audio_assets", "asset_kind", "TEXT NOT NULL DEFAULT 'voice'")
        _ensure_column(c, "audio_assets", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
        c.execute("""INSERT OR REPLACE INTO audio_assets
            (job_id, username, voice_id, voice_key, file, url, text, speed, pitch, volume, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, username, voice_id, voice_key, result.get("file"), result.get("url"),
             result.get("text") or result.get("prompt"), result.get("speed"), result.get("pitch"),
             result.get("volume"), now))
        c.execute(
            "UPDATE audio_assets SET asset_kind=?,metadata_json=? WHERE job_id=?",
            (
                asset_kind,
                json.dumps({
                    "provider": result.get("provider"),
                    "provider_model": result.get("provider_model"),
                    "provider_request_id": result.get("provider_request_id"),
                    "quality": result.get("quality") or {},
                    "sound_design": result.get("sound_design") or {},
                    "duration_ms": result.get("duration_ms"),
                }, ensure_ascii=False, sort_keys=True),
                job_id,
            ),
        )
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

def _migrate_public_voice_presets():
    """切换公共音色到 CosyVoice，并让旧供应商生成的试听只失效一次。"""
    now = int(time.time())
    changed = 0
    with closing(adb()) as c:
        for legacy_voice, preset_voice in cosyvoice.PUBLIC_VOICE_PRESETS.items():
            changed += c.execute("""UPDATE audio_voices
                SET provider_voice=?, preview_file=NULL, preview_url=NULL, updated_at=?
                WHERE scope='public' AND username='' AND voice_key=? AND provider_voice=?""",
                (preset_voice, now, legacy_voice, legacy_voice)).rowcount
        c.commit()
    if changed:
        print("[audio] 公共音色已切换 CosyVoice，旧试听缓存失效 %d 条" % changed, flush=True)
    return changed

def _repair_ready_cosyvoice_slots():
    """试听已生成即代表音色可用；把历史遗留的 training 状态幂等落成 ready。"""
    with closing(adb()) as c:
        changed = c.execute("""UPDATE audio_voice_slots
            SET status='ready', updated_at=?
            WHERE status='training' AND EXISTS (
                SELECT 1 FROM audio_voices v
                WHERE v.id=audio_voice_slots.voice_id
                  AND v.scope='personal'
                  AND v.provider_voice LIKE 'cosyvoice-%'
                  AND COALESCE(v.preview_url, '')<>''
            )""", (int(time.time()),)).rowcount
        c.commit()
    if changed:
        print("[audio] 修正已就绪 CosyVoice 槽位 %d 条" % changed, flush=True)
    return changed

def backfill_audio_assets():
    _cleanup_alloy_placeholder_voices()   # #604: 先清存量占位，再重放(ensure 已不再重建)
    _migrate_public_voice_presets()
    _repair_ready_cosyvoice_slots()
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
    if not cosyvoice.enabled():
        return d
    try:
        audio_bytes = cosyvoice.synth(_cosy_voice_for(speaker), PUBLIC_VOICE_SAMPLE_TEXT)
        fn = "audio/voice_preview_%s.mp3" % uuid.uuid4().hex
        _out_path(fn).write_bytes(audio_bytes)
        url = public_url(fn, "audio/mpeg")
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
        rows = c.execute("""SELECT v.id, v.scope, v.username, v.voice_key,
                v.display_name, v.provider_voice, v.preview_file, v.preview_url,
                v.slot_id, v.created_at, v.updated_at,
                COALESCE(s.status, '') AS slot_status
            FROM audio_voices AS v
            LEFT JOIN audio_voice_slots AS s
              ON v.scope='personal' AND s.username=v.username
             AND s.slot_id=v.slot_id AND s.voice_id=v.id
            WHERE v.scope='public' OR (v.scope='personal' AND v.username=?)
            ORDER BY CASE v.scope WHEN 'public' THEN 0 ELSE 1 END, v.id""",
            (username,)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        slot_status = item.pop("slot_status", "")
        is_public = item.get("scope") == "public"
        item["status"] = "ready" if is_public else slot_status
        item["ready"] = bool(
            is_public or (
                slot_status == "ready"
                and str(item.get("provider_voice") or "").startswith(
                    cosyvoice.CLONE_MODEL
                )
            )
        )
        items.append(item)
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

def list_audio_assets(username, limit=120, offset=0):
    limit = max(1, min(120, int(limit or 120)))
    offset = max(0, min(100000, int(offset or 0)))
    with closing(adb()) as c:
        _ensure_column(c, "audio_assets", "deleted", "INTEGER DEFAULT 0")
        _ensure_column(c, "audio_assets", "asset_kind", "TEXT NOT NULL DEFAULT 'voice'")
        _ensure_column(c, "audio_assets", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
        rows = c.execute("""SELECT a.id, a.job_id, a.username, a.voice_id, a.voice_key, a.file, a.url, a.text,
                   a.speed, a.pitch, a.volume, a.created_at, v.display_name AS voice_name, v.preview_url
                   ,a.asset_kind,a.metadata_json
            FROM audio_assets a
            LEFT JOIN audio_voices v ON v.id = a.voice_id
            WHERE a.username=? AND COALESCE(a.deleted,0)=0
            ORDER BY a.id DESC LIMIT ? OFFSET ?""", (username, limit, offset)).fetchall()
    return [dict(r) for r in rows]


def get_audio_asset_by_job(username, job_id):
    try:
        job_id = int(job_id)
    except (TypeError, ValueError):
        return None
    with closing(adb()) as conn:
        _ensure_column(conn, "audio_assets", "deleted", "INTEGER DEFAULT 0")
        _ensure_column(conn, "audio_assets", "asset_kind", "TEXT NOT NULL DEFAULT 'voice'")
        _ensure_column(conn, "audio_assets", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
        row = conn.execute(
            "SELECT id,job_id,username,file,url,text,created_at,asset_kind,"
            "metadata_json FROM audio_assets WHERE job_id=? AND username=? "
            "AND COALESCE(deleted,0)=0", (job_id, username),
        ).fetchone()
    return dict(row) if row else None


def get_audio_asset(username, asset_id):
    """Return one owned, non-deleted audio asset for internal composition use."""
    try:
        asset_id = int(asset_id)
    except (TypeError, ValueError):
        return None
    with closing(adb()) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_column(conn, "audio_assets", "deleted", "INTEGER DEFAULT 0")
        row = conn.execute(
            "SELECT id,job_id,username,file,url,created_at FROM audio_assets "
            "WHERE id=? AND username=? AND COALESCE(deleted,0)=0",
            (asset_id, username),
        ).fetchone()
    return dict(row) if row else None

# ============ 配音能力：OpenAI TTS（同事的 audio 能力，合并保留） ============
VOICE_MAP = {
    "personal": os.environ.get("VOICE_PERSONAL", "alloy"),
    "alloy": "alloy", "ash": "ash", "ballad": "ballad", "coral": "coral", "echo": "echo",
    "fable": "fable", "nova": "nova", "onyx": "onyx", "sage": "sage", "shimmer": "shimmer",
}
SPEED_MAP = {"slow": 0.88, "normal": 1.0, "fast": 1.12, "偏慢": 0.88, "正常": 1.0, "偏快": 1.12}


def _audio_duration_ms(file_name):
    """Return authoritative ffprobe duration, or None for metadata recovery."""
    try:
        path = _out_path(file_name)
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            check=True, timeout=30, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        duration_ms = int(round(float(proc.stdout.strip()) * 1000))
        return duration_ms if duration_ms > 0 else None
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return None


def _audio_result(file_name, voice_key, speed, pitch, volume, text, publish=True):
    result = {
        "type": "audio", "file": file_name,
        "voice": voice_key, "speed": speed, "pitch": pitch, "volume": volume,
        "text": text, "prompt": text,
        "duration_ms": _audio_duration_ms(file_name),
    }
    if publish:
        result["url"] = public_url(file_name, "audio/mpeg")
    return result


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


def _resolve_owned_ready_personal_voice(username, voice_key):
    username = str(username or "").strip()
    voice_key = str(voice_key or "").strip()
    if not username or len(username) > 128 or not voice_key or len(voice_key) > 128:
        raise ValueError("个人音色不存在或不可用")

    with closing(adb()) as c:
        row = c.execute(
            """
            SELECT v.provider_voice
            FROM audio_voices AS v
            INNER JOIN audio_voice_slots AS s
                ON s.voice_id = v.id
               AND s.username = v.username
               AND s.slot_id = v.slot_id
            WHERE v.scope = 'personal'
              AND v.username = ?
              AND v.voice_key = ?
              AND s.status = 'ready'
            LIMIT 1
            """,
            (username, voice_key),
        ).fetchone()

    if not row:
        raise ValueError("个人音色不存在或不可用")

    provider_voice = _cosy_voice_for(row["provider_voice"])
    if not str(provider_voice).startswith(cosyvoice.CLONE_MODEL):
        raise ValueError("个人音色不存在或不可用")
    return provider_voice


def require_owned_ready_personal_voice(username, voice_key):
    """Validate ownership/readiness without exposing the provider voice ID."""
    _resolve_owned_ready_personal_voice(username, voice_key)

def validate_audio_payload(payload, username=""):
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    body = dict(payload)
    text = str(body.get("text") or body.get("prompt") or "").strip()
    if not text:
        raise ValueError("配音文案不能为空")
    if len(text) > 1000:
        raise ValueError("配音文案过长，请控制在 1000 字以内")
    raw_voice_key = body.get("voice") or "S_d21F8OR62"
    if not isinstance(raw_voice_key, str) or not raw_voice_key.strip() or len(raw_voice_key.strip()) > 128:
        raise ValueError("音色参数无效")
    voice_key = raw_voice_key.strip()
    bound_provider_voice = str(
        body.get("digital_human_provider_voice") or ""
    ).strip()
    if bound_provider_voice:
        try:
            bound_voice_id = int(body.get("digital_human_voice_id") or 0)
            bound_voice_updated_at = int(
                body.get("digital_human_voice_updated_at") or 0
            )
        except (TypeError, ValueError):
            bound_voice_id = 0
            bound_voice_updated_at = 0
        if (body.get("digital_human_pipeline") != "digital_human_video_voice"
                or not str(body.get("digital_human_consent_id") or "").startswith("dhvc_")
                or bound_voice_id <= 0 or bound_voice_updated_at <= 0
                or len(bound_provider_voice) > 256):
            raise ValueError("完整配音的音色版本绑定无效，请重新授权")
    elif username:
        resolve_audio_provider_voice(username, voice_key)
        # Keep Tang's customer-audio audit metadata without exposing the
        # provider voice identifier.
        body["voice_scope"] = (
            "public" if voice_key in cosyvoice.PUBLIC_VOICE_PRESETS else "personal"
        )
        body["provider"] = "cosyvoice"
    raw_speed = body.get("speed")
    if isinstance(raw_speed, (int, float)):
        if isinstance(raw_speed, bool) or raw_speed != raw_speed or not 0.5 <= float(raw_speed) <= 2.0:
            raise ValueError("语速必须是 0.5-2.0")
        speed = round(float(raw_speed), 1)
    else:
        if raw_speed not in (None, "", *SPEED_MAP):
            raise ValueError("语速参数无效")
        speed = SPEED_MAP.get(raw_speed or "normal", 1.0)
    for name, minimum, maximum in (("pitch", -12, 12), ("volume", -50, 100)):
        raw = body.get(name, 0)
        if isinstance(raw, bool):
            raise ValueError("%s 必须是整数" % name)
        if isinstance(raw, float):
            if raw != raw or not raw.is_integer():
                raise ValueError("%s 必须是整数" % name)
            clean = int(raw)
            if not minimum <= clean <= maximum:
                raise ValueError("%s 超出范围" % name)
            body[name] = clean
            continue
        try:
            clean = int(raw)
        except (TypeError, ValueError):
            raise ValueError("%s 必须是整数" % name)
        if str(raw).strip() != str(clean) or not minimum <= clean <= maximum:
            raise ValueError("%s 超出范围" % name)
        body[name] = clean
    body.update({
        "text": text,
        "voice": voice_key,
        "speed": speed,
        "delivery": normalize_audio_delivery(body.get("delivery")),
    })
    return body


def synthesize_owned_voice_segment(
    username,
    voice_key,
    text,
    speed=1.0,
    pitch=0,
    volume=0,
):
    payload = validate_audio_payload(
        {
            "text": text,
            "voice": voice_key,
            "speed": speed,
            "pitch": pitch,
            "volume": volume,
        }
    )
    provider_voice = _resolve_owned_ready_personal_voice(username, voice_key)
    if not cosyvoice.enabled():
        raise ValueError("声音服务暂时不可用，请稍后重试")

    content = cosyvoice.synth(
        provider_voice,
        payload["text"],
        rate=payload["speed"],
        pitch=max(0.5, min(2.0, 1.0 + payload["pitch"] / 24.0)),
        volume=max(0, min(100, 50 + payload["volume"] // 2)),
    )
    if not content:
        raise RuntimeError("CosyVoice 合成返回为空")

    return {
        "content": content,
        "content_type": "audio/mpeg",
        "voice_key": str(voice_key).strip(),
        "voice_scope": "personal",
        "provider": "cosyvoice",
    }


COSY_SYNTH_RETRY_DELAYS = (1, 2)


def _cosy_synth_for_generation(voice, text, **kwargs):
    """Retry only the provider's known transient 530 failure in this job."""
    total_attempts = len(COSY_SYNTH_RETRY_DELAYS) + 1
    for attempt in range(total_attempts):
        try:
            return cosyvoice.synth(voice, text, **kwargs)
        except cosyvoice.CosyVoiceTaskError as error:
            if not error.retryable:
                raise
            attempt_number = attempt + 1
            print(
                "[cosyvoice] transient synthesis failure "
                "code=%s attempt=%d/%d"
                % (error.code or "unknown", attempt_number, total_attempts)
            )
            if attempt >= len(COSY_SYNTH_RETRY_DELAYS):
                raise RuntimeError(
                    "语音合成服务暂时繁忙，系统自动重试后仍未成功，请点击继续重试"
                ) from None
            time.sleep(COSY_SYNTH_RETRY_DELAYS[attempt])
    raise RuntimeError("语音合成服务暂时繁忙，请点击继续重试")


def gen_audio(payload, publish=True):
    """Generate one audio file.

    ``publish=False`` is reserved for server-side composition intermediates so
    per-scene TTS files are not uploaded as orphaned public assets.  Public API
    callers keep the historical one-argument behavior.
    """
    username = str((payload or {}).get("_username") or "").strip()
    payload = validate_audio_payload(payload, username)
    text = payload["text"]
    voice_key = payload["voice"]
    voice = (str(payload.get("digital_human_provider_voice") or "").strip()
             or resolve_audio_provider_voice(username, voice_key))
    speed, pitch, volume = payload["speed"], payload["pitch"], payload["volume"]
    delivery = payload["delivery"]

    # Current public and personal voices use CosyVoice. Never fall back to
    # the retired provider when the CosyVoice channel is unavailable.
    if cosyvoice.enabled():
        cv_voice = _cosy_voice_for(voice)
        # knob 的 pitch/-12~12、volume/-50~100 是豆包量纲；CosyVoice 用 pitch 0.5~2、volume 0~100。
        # Personal one-click voices use cosyvoice-v3.5-plus, which supports
        # free-form instruction control.  Public legacy preset voices run on
        # cosyvoice-v1, so never send an unsupported instruction to them.
        instruction = DELIVERY_INSTRUCTIONS[delivery] if str(cv_voice).startswith(cosyvoice.CLONE_MODEL) else ""
        cv_audio = _cosy_synth_for_generation(
            cv_voice, text, rate=speed,
            pitch=max(0.5, min(2.0, 1.0 + pitch / 24.0)),
            volume=max(0, min(100, 50 + volume // 2)),
            instruction=instruction,
        )
        fn = "audio/aud_%d.mp3" % int(time.time() * 1000)   # 非敏感命名 → 可走 COS 公开直链
        _out_path(fn).write_bytes(cv_audio)
        return _audio_result(fn, voice_key, speed, pitch, volume, text, publish=publish)

    if voice_key.startswith(("S_", "vip_")) or str(voice).startswith(("S_", "vip_", "cosyvoice-")):
        raise ValueError("声音服务暂不可用，请稍后重试")

    instructions = "中文短视频口播配音，语气自然，吐字清晰，节奏适合美业/本地生活转化。"
    body = json.dumps({
        "model": TTS_MODEL, "voice": voice, "input": text,
        "instructions": instructions, "response_format": "mp3", "speed": speed,
    }, ensure_ascii=False).encode()
    data = _post_bytes("/v1/audio/speech", body, "application/json")
    fn = "audio/aud_%d.mp3" % int(time.time() * 1000)
    _out_path(fn).write_bytes(data)
    return _audio_result(fn, voice_key, speed, pitch, volume, text, publish=publish)

HANDLERS = {"audio": gen_audio}
