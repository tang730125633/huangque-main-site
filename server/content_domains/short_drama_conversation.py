"""Conversation-first script planning for the standalone short-drama studio.

PR-2 deliberately keeps this free of billing and production side effects.  A
locked snapshot is the hand-off contract consumed by the later preflight
phase; the legacy canvas production tables remain untouched.
"""

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing

from . import short_drama_storyboard, short_drama_duration


STATES = {
    "idea_intake",
    "direction_review",
    "script_generating",
    "script_review",
    "script_locked",
    "generation_failed",
}
VERSION_STATUSES = {"draft", "locked", "superseded"}
MAX_MESSAGE_LENGTH = 8000
MAX_VERSIONS = 30


class ConversationError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = int(status)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_conversations (
  project_id TEXT PRIMARY KEY REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  state TEXT NOT NULL CHECK(state IN
    ('idea_intake','direction_review','script_generating','script_review',
     'script_locked','generation_failed')),
  understanding_json TEXT NOT NULL DEFAULT '{}',
  current_version_id TEXT,
  locked_version_id TEXT,
  revision INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS short_drama_conversation_messages (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
  content TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_short_drama_conversation_messages
  ON short_drama_conversation_messages(project_id, created_at, id);

CREATE TABLE IF NOT EXISTS short_drama_conversation_requests (
  id TEXT PRIMARY KEY,
  actor_username TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  operation TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE(actor_username, project_id, operation, idempotency_key)
);

CREATE TABLE IF NOT EXISTS short_drama_conversation_jobs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  actor_username TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('script_generate')),
  status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed')),
  request_json TEXT NOT NULL,
  result_version_id TEXT,
  error_json TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_short_drama_conversation_jobs_project
  ON short_drama_conversation_jobs(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS short_drama_script_snapshots (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  parent_id TEXT,
  status TEXT NOT NULL CHECK(status IN ('draft','locked','superseded')),
  script_json TEXT NOT NULL,
  readable_text TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  provider TEXT NOT NULL,
  model_version TEXT NOT NULL,
  instruction TEXT NOT NULL DEFAULT '',
  change_summary TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  locked_by TEXT,
  locked_at INTEGER,
  UNIQUE(project_id, version)
);
CREATE INDEX IF NOT EXISTS idx_short_drama_script_snapshots_project
  ON short_drama_script_snapshots(project_id, version DESC);
"""


def _connection(db_factory):
    conn = db_factory()
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _json(value, fallback):
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return fallback
    return parsed


def _json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json_text(value).encode("utf-8")).hexdigest()


def _idempotency_key(value):
    value = str(value or "").strip()
    if not value or len(value) > 160:
        raise ConversationError("idempotency_key_required", "缺少有效的幂等键")
    return value


def _request_revision(body):
    revision = body.get("conversation_revision")
    if type(revision) is not int or revision < 1:
        raise ConversationError("conversation_revision_invalid", "对话版本无效")
    return revision


def _message(value):
    value = str(value or "").strip()
    if not value:
        raise ConversationError("message_required", "请输入创作要求")
    if len(value) > MAX_MESSAGE_LENGTH:
        raise ConversationError("message_too_long", "单条消息不能超过 8000 个字符")
    return value


def init_db(db_factory):
    conn = _connection(db_factory)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _project(conn, owner_username, project_id):
    cursor = conn.execute(
        "SELECT id,title,synopsis,ratio,target_duration,shot_count,visual_style,"
        "target_platform,stage,revision FROM short_drama_projects "
        "WHERE id=? AND username=? AND deleted=0",
        (project_id, owner_username),
    )
    values = cursor.fetchone()
    if not values:
        raise LookupError("short drama project does not exist")
    return dict(zip((column[0] for column in cursor.description), values))


def _ensure_conversation(conn, project_id):
    now = int(time.time())
    conn.execute(
        "INSERT OR IGNORE INTO short_drama_conversations "
        "(project_id,state,understanding_json,revision,created_at,updated_at) "
        "VALUES (?,'idea_intake','{}',1,?,?)",
        (project_id, now, now),
    )


def _conversation(conn, project_id):
    cursor = conn.execute(
        "SELECT * FROM short_drama_conversations WHERE project_id=?", (project_id,)
    )
    values = cursor.fetchone()
    return dict(zip((column[0] for column in cursor.description), values))


def _snapshot(row):
    if not row:
        return None
    item = dict(row)
    item["script"] = _json(item.pop("script_json"), {})
    item["version"] = int(item["version"])
    return item


def _snapshot_by_id(conn, project_id, version_id):
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM short_drama_script_snapshots WHERE project_id=? AND id=?",
        (project_id, version_id),
    ).fetchone()
    return _snapshot(row)


_GREETING_RE = re.compile(r"^(你好|您好|嗨|哈喽|hello|hi|在吗)[！!。.，,\s]*$", re.I)
_RECOMMEND_RE = re.compile(
    r"(推荐|建议|怎么写|怎么拍|剧情.{0,6}(怎么样|如何)|给.{0,16}(方案|方向)|"
    r"(三个|3个).{0,8}(方案|方向)|剧情方向)"
)
_CONFIRM_RE = re.compile(r"^(确认|就这个|就按这个|按这个|可以|确定|没问题|采用|选定)")
_IMPORT_PURE_CONFIRM_RE = re.compile(
    r"^(?:确认(?:尊重原稿并生成|优化范围|调整后的(?:原稿理解|优化范围|保留范围))?"
    r"|确定|没问题|就这个|就按这个|按这个|采用|选定|可以)[。！!\s]*$"
)
_QUESTION_RE = re.compile(r"[？?]|(?:吗|么|是否|能否|可否)[。！!\s]*$")
_REVISION_RE = re.compile(
    r"(修改|调整|改成|换成|补充|加强|减弱|不要|希望|需要|结尾|人物|主角|冲突|"
    r"情绪|风格|基调|设定|场景|对白|台词|反转|节奏)"
)
_SELECTIONS = (
    (re.compile(r"(方案\s*[一1]|第\s*[一1]\s*个|情感治愈)"), "emotion"),
    (re.compile(r"(方案\s*[二2]|第\s*[二2]\s*个|冲突反转)"), "twist"),
    (re.compile(r"(方案\s*[三3]|第\s*[三3]\s*个|成长抉择)"), "growth"),
)


def _user_messages(messages):
    return [
        str(item.get("content") or "").strip()
        for item in messages
        if item.get("role") == "user" and str(item.get("content") or "").strip()
    ]


def _selected_recommendation(user_messages):
    for value in reversed(user_messages):
        for pattern, recommendation_id in _SELECTIONS:
            if pattern.search(value):
                return recommendation_id
    return ""


def _conversation_decision(user_messages):
    """Derive a durable selection/confirmation state from the full user history."""
    selected_id = ""
    selected_at = -1
    confirmed_at = -1
    invalidated_at = -1
    for index, value in enumerate(user_messages):
        selection = _selected_recommendation([value])
        if selection:
            selected_id = selection
            selected_at = index
            confirmed_at = -1
            invalidated_at = -1
            continue
        if selected_id and _CONFIRM_RE.match(value):
            confirmed_at = index
            invalidated_at = -1
            continue
        if (
            confirmed_at >= 0
            and index > confirmed_at
            and not _GREETING_RE.match(value)
            and not _RECOMMEND_RE.search(value)
            and _REVISION_RE.search(value)
        ):
            invalidated_at = index
    return {
        "selected_recommendation_id": selected_id,
        "selected_at": selected_at,
        "confirmed_at": confirmed_at,
        "invalidated_at": invalidated_at,
        "direction_confirmed": bool(
            selected_id and confirmed_at >= 0 and invalidated_at < confirmed_at
        ),
    }


def _extract_choice(values, choices, fallback=""):
    joined = " ".join(values)
    for choice in choices:
        if choice in joined:
            return choice
    return fallback


def _story_notes(user_messages):
    notes = []
    for value in user_messages:
        if _GREETING_RE.match(value):
            continue
        if _CONFIRM_RE.match(value) or _selected_recommendation([value]):
            continue
        cleaned = re.sub(r"^(你好|您好|嗨|哈喽)[！!。.，,\s]*", "", value, flags=re.I)
        cleaned = re.sub(
            r"(你先)?(给我|帮我).{0,20}(方案|方向)|(请)?推荐.{0,20}(方案|方向)",
            "",
            cleaned,
        )
        cleaned = re.sub(r"(具体的?)?剧情(是)?怎么样|你的推荐呢", "", cleaned)
        cleaned = re.sub(r"^(我想|我希望|希望|请|可以)\s*", "", cleaned).strip(" ，。？?")
        cleaned = re.sub(r"(?:请|麻烦)\s*$", "", cleaned).strip(" ，。？！?")
        if cleaned and cleaned not in notes:
            notes.append(cleaned)
    return notes[-8:]


def _recommendations(project, tone, ending, premise=""):
    premise = str(premise or project["synopsis"] or "").strip()
    subject = premise[:56] or "主人公面对一次意外事件"
    return [
        {
            "id": "emotion",
            "title": "方案一 · 情感治愈",
            "hook": "先用关系裂痕抓住观众，再用一个细节完成和解。",
            "summary": "围绕“%s”，把重点放在人物关系和情绪递进，结尾落在%s。"
            % (subject, ending or "温暖但不过度煽情"),
            "tone": tone or "温暖克制",
            "ending": ending or "温暖收束",
        },
        {
            "id": "twist",
            "title": "方案二 · 冲突反转",
            "hook": "开场直接抛出异常线索，中段误导，最后一镜兑现反转。",
            "summary": "围绕“%s”，强化目标阻力和信息差，让真相改变观众对前文的理解。"
            % subject,
            "tone": tone or "悬疑紧张",
            "ending": ending or "有依据的反转",
        },
        {
            "id": "growth",
            "title": "方案三 · 成长抉择",
            "hook": "让主人公必须在两个都有代价的选择之间作决定。",
            "summary": "围绕“%s”，用一次关键选择推动人物成长，结尾留下清晰余味。"
            % subject,
            "tone": tone or "真实有力量",
            "ending": ending or "克制开放",
        },
    ]


def _understanding(project, messages):
    user_messages = _user_messages(messages)
    latest = user_messages[-1] if user_messages else ""
    notes = _story_notes(user_messages)
    tone = _extract_choice(
        user_messages,
        ("温暖", "治愈", "悬疑", "紧张", "轻喜剧", "喜剧", "热血", "压抑", "写实"),
        str(project["visual_style"] or ""),
    )
    ending = _extract_choice(
        user_messages,
        ("不要悲剧", "温暖反转", "反转", "圆满", "开放式", "遗憾", "悲剧"),
        "",
    )
    protagonist = ""
    joined = " ".join(user_messages)
    name_match = re.search(r"主角(?:叫|是)\s*([\u4e00-\u9fffA-Za-z0-9·]{2,12})", joined)
    if name_match:
        protagonist = name_match.group(1)
    decision = _conversation_decision(user_messages)
    selected_id = decision["selected_recommendation_id"]
    creative_brief = str(project["synopsis"] or "").strip()
    if notes:
        creative_brief += "；补充要求：" + "；".join(notes)
    recommendations = _recommendations(project, tone, ending, creative_brief)
    selected = next(
        (item for item in recommendations if item["id"] == selected_id), None
    )
    confirmed = bool(selected and decision["direction_confirmed"])
    confirmation_invalidated = bool(
        selected and decision["invalidated_at"] > decision["confirmed_at"] >= 0
    )
    wants_recommendation = bool(_RECOMMEND_RE.search(latest))
    if confirmed:
        phase = "direction_ready"
    elif confirmation_invalidated:
        phase = "refining"
    elif selected or wants_recommendation:
        phase = "recommending"
    else:
        phase = "discovering"
    missing = []
    if not protagonist:
        missing.append("主角")
    if not tone or tone == project["visual_style"]:
        missing.append("情绪基调")
    if not ending:
        missing.append("结局方向")
    if selected:
        creative_brief += "；采用%s：%s" % (selected["title"], selected["summary"])
    open_questions = []
    if not confirmed:
        if confirmation_invalidated:
            open_questions.append("我已记录新的修改要求，是否按调整后的方向重新确认？")
        elif selected:
            open_questions.append("是否确认采用这个方向，还是需要调整人物、冲突或结局？")
        elif wants_recommendation:
            open_questions.append("请选择一个推荐方向，我会继续细化。")
        elif missing:
            open_questions.append("接下来想先确定%s吗？" % missing[0])
    return {
        "premise": project["synopsis"],
        "creative_brief": creative_brief[:2400],
        "audience": "短视频观众",
        "tone": tone,
        "ending": ending,
        "protagonist": protagonist,
        "platform": project["target_platform"],
        "duration_seconds": short_drama_duration.choose(
            project["target_duration"], project.get("shot_count") or 0
        ),
        "duration_range_seconds": list(
            short_drama_duration.bounds(project["target_duration"])
        ),
        "ratio": project["ratio"],
        "latest_request": latest[-1200:],
        "story_notes": notes,
        "user_requirements": {
            "story_notes": notes,
            "protagonist": protagonist,
            "tone": tone,
            "ending": ending,
        },
        "selected_direction": selected or {},
        "phase": phase,
        "missing_fields": missing,
        "recommendations": recommendations if wants_recommendation or selected else [],
        "selected_recommendation_id": selected_id,
        "direction_confirmed": confirmed,
        "confirmation_invalidated": confirmation_invalidated,
        "ready_to_generate": confirmed,
        "open_questions": open_questions,
    }


def _assistant_reply(project, understanding):
    latest = understanding["latest_request"]
    recommendations = understanding["recommendations"]
    selected_id = understanding["selected_recommendation_id"]
    selected = next((item for item in recommendations if item["id"] == selected_id), None)
    metadata = {
        "kind": "creative_guidance",
        "phase": understanding["phase"],
        "recommendations": recommendations,
        "quick_replies": [],
    }
    if _GREETING_RE.match(latest):
        metadata["quick_replies"] = ["帮我推荐三个方向", "我想做悬疑反转", "我想做温暖治愈"]
        return (
            "你好，我会先理解你的故事想法，再帮你补齐人物、冲突和结局。"
            "你可以先说一句核心设定，也可以直接让我推荐三个方向。",
            metadata,
        )
    if understanding["direction_confirmed"]:
        metadata["kind"] = "direction_confirmed"
        metadata["recommendations"] = []
        metadata["quick_replies"] = ["补充人物设定", "补充结局要求"]
        return (
            "方向已确认。我会以“%s”为故事底稿，按 %s 秒、%s 的规格组织成不同功能的镜头，"
            "不会把我们的聊天提问直接写成台词。现在可以生成剧本，也可以再补充一条硬性要求。"
            % (
                understanding["creative_brief"][:180],
                project["target_duration"],
                project["ratio"],
            ),
            metadata,
        )
    if understanding.get("confirmation_invalidated"):
        metadata["kind"] = "direction_refined"
        metadata["recommendations"] = []
        metadata["quick_replies"] = ["确认调整后的方向", "继续补充人物", "继续调整结局"]
        return (
            "我已把“%s”记录为新的创作约束。因为方向内容发生了变化，"
            "之前的确认已自动失效；请检查理解摘要，确认无误后重新确认方向。"
            % latest[:120],
            metadata,
        )
    if selected:
        metadata["kind"] = "recommendation_selected"
        metadata["recommendations"] = []
        metadata["quick_replies"] = ["确认这个方向", "加强人物冲突", "结尾再温暖一点"]
        if not _selected_recommendation([latest]):
            return (
                "我已把“%s”加入%s的创作约束。当前方向仍然有效；"
                "你可以继续调整，也可以确认后生成剧本。"
                % (latest[:120], selected["title"]),
                metadata,
            )
        return (
            "你选择了%s。我的细化建议是：%s %s "
            "如果方向符合预期，请点“确认这个方向”；如果不满意，可以继续告诉我想改哪一部分。"
            % (selected["title"], selected["hook"], selected["summary"]),
            metadata,
        )
    if recommendations:
        metadata["kind"] = "recommendations"
        metadata["quick_replies"] = ["方案一 · 情感治愈", "方案二 · 冲突反转", "方案三 · 成长抉择"]
        return (
            "我根据当前核心故事整理了三个可落地的方向。它们不是最终剧本，"
            "而是三种不同的叙事选择；选中一个后，我会继续细化人物、冲突和结局。",
            metadata,
        )
    question = understanding["open_questions"][0] if understanding["open_questions"] else ""
    metadata["quick_replies"] = ["帮我推荐三个方向", "情绪要温暖克制", "结尾需要合理反转"]
    return (
        "我已经记下你的新要求，并会把它作为创作约束，而不是直接复制成角色台词。"
        + (question or "你还可以继续补充人物、冲突或结局。"),
        metadata,
    )


def _story_clauses(project, understanding, source_import=None):
    values = []
    if source_import:
        values.append(str(source_import.get("source_text") or "").strip())
    values.append(str(project.get("synopsis") or "").strip())
    values.extend(
        str(item).strip()
        for item in understanding.get("story_notes", [])
        if str(item).strip()
    )
    clauses = []
    for value in values:
        for clause in re.split(r"[。！？!?；;\n]+", value):
            clause = clause.strip(" ，、：:")
            if not clause or _RECOMMEND_RE.search(clause):
                continue
            if clause.startswith(("补充要求", "采用方案", "确认这个方向")):
                continue
            if clause not in clauses:
                clauses.append(clause)
    return clauses or [str(project.get("synopsis") or "故事发生").strip()]


def _character_names(seed, understanding):
    protagonist = str(understanding.get("protagonist") or "").strip()
    relationship_pairs = (
        (("母女", "母亲", "妈妈", "女儿"), ("女儿", "母亲")),
        (("父子", "父亲", "爸爸", "儿子"), ("儿子", "父亲")),
        (("父女",), ("女儿", "父亲")),
        (("母子",), ("儿子", "母亲")),
        (("夫妻", "丈夫", "妻子"), ("妻子", "丈夫")),
        (("姐妹",), ("姐姐", "妹妹")),
        (("兄弟",), ("哥哥", "弟弟")),
        (("旧友", "朋友"), ("朋友甲", "朋友乙")),
    )
    names = []
    if protagonist:
        names.append(protagonist)
    for keywords, pair in relationship_pairs:
        if any(keyword in seed for keyword in keywords):
            names.extend(pair)
            break
    if "记者" in seed and "记者" not in names:
        names.insert(0, "记者")
    if not names:
        names = ["主角", "关键人物"]
    unique = []
    for name in names:
        if name and name not in unique:
            unique.append(name)
    return unique[:3]


_EXPLICIT_SHOT_RE = re.compile(
    r"(?im)^\s*(?:镜头|分镜)\s*#?\s*(\d+)\s*"
    r"(?:[（(]\s*(?:(\d+(?:\.\d+)?)\s*(?:秒|s)?\s*[-—~至]\s*"
    r"(\d+(?:\.\d+)?)\s*(?:秒|s)?|(\d+(?:\.\d+)?)\s*(?:秒|s))\s*[）)])?"
    r"\s*[:：、,，]?\s*(.*)$"
)
_CAMERA_TERMS = (
    "大特写", "特写", "近景", "中近景", "中景", "中远景", "远景", "全景",
    "半身镜头", "半身", "俯拍", "仰拍", "航拍", "跟拍", "手持", "固定镜头",
)


def _explicit_storyboard_requirements(source):
    """Extract user-authored numbered shots without interpreting prose mentions."""
    source = str(source or "")
    matches = list(_EXPLICIT_SHOT_RE.finditer(source))
    requirements = []
    for position, match in enumerate(matches):
        number = int(match.group(1))
        block_end = matches[position + 1].start() if position + 1 < len(matches) else len(source)
        tail = match.group(5).strip()
        continuation = source[match.end():block_end].strip()
        content = "\n".join(value for value in (tail, continuation) if value).strip()
        start = float(match.group(2)) if match.group(2) else None
        end = float(match.group(3)) if match.group(3) else None
        duration = (
            max(1, int(round(end - start))) if start is not None and end is not None
            else max(1, int(round(float(match.group(4))))) if match.group(4) else None
        )
        camera = next((term for term in _CAMERA_TERMS if term in tail[:30]), "")
        visual = content
        if camera and visual.startswith(camera):
            visual = visual[len(camera):].lstrip(" ，,、：:")
        requirements.append({
            "number": number, "duration_seconds": duration,
            "camera": camera, "visual": visual or content,
            "source_text": content, "source_offset": match.start(),
        })
    return requirements


def _apply_explicit_storyboard(script, project, source):
    requirements = _explicit_storyboard_requirements(source)
    if not requirements:
        for shot in script.get("shots") or []:
            shot["source_type"] = "system_generated"
        return script
    shots = script.get("shots") or []
    beats = script.get("story_beats") or []
    lines = script.get("dialogue_lines") or []
    characters = script.get("characters") or []
    by_number = {
        item["number"]: item for item in requirements
        if 1 <= item["number"] <= len(shots)
    }
    fixed = {
        number - 1: int(item["duration_seconds"])
        for number, item in by_number.items() if item.get("duration_seconds")
    }
    public_band = project.get("target_duration") in short_drama_duration.DURATION_BANDS
    if public_band and any(
        duration not in short_drama_duration.SHOT_DURATION_SECONDS
        for duration in fixed.values()
    ):
        raise ConversationError(
            "explicit_storyboard_duration_invalid",
            "用户填写的单镜时长必须为 5 或 10 秒", 422,
        )
    authored_total = sum(fixed.values())
    target = short_drama_duration.choose(
        project.get("target_duration"), len(shots), authored_total
    )
    flexible = [index for index in range(len(shots)) if index not in fixed]
    remaining = target - sum(fixed.values())
    if remaining < len(flexible) or (not flexible and remaining != 0):
        raise ConversationError(
            "explicit_storyboard_duration_mismatch",
            "用户填写的分镜总时长与项目目标时长不一致，请调整分镜时间或项目时长后重试",
            422,
        )
    if flexible and public_band:
        long_count, remainder = divmod(remaining - len(flexible) * 5, 5)
        if remainder or long_count < 0 or long_count > len(flexible):
            raise ConversationError(
                "explicit_storyboard_duration_mismatch",
                "用户填写的分镜时长无法与其余 5/10 秒镜头组成所选时长", 422,
            )
        allocated = [5] * (len(flexible) - long_count) + [10] * long_count
        for index, duration in zip(flexible, allocated):
            shots[index]["duration_seconds"] = duration
    elif flexible:
        allocated = short_drama_storyboard.allocate_durations(
            remaining, len(flexible),
        )
        for index, duration in zip(flexible, allocated):
            shots[index]["duration_seconds"] = duration
    for index, duration in fixed.items():
        shots[index]["duration_seconds"] = duration
    for index, shot in enumerate(shots):
        requirement = by_number.get(index + 1)
        if not requirement:
            shot["source_type"] = "system_generated"
            continue
        visual = requirement["visual"] or shot.get("visual") or ""
        scene = short_drama_storyboard._location(visual, shot.get("scene") or "故事主要场景")
        visible = [item for item in characters if str(item.get("name") or "") in visual]
        if not visible:
            visible = [
                item for item in characters
                if item.get("character_key") in (shot.get("character_keys") or [])
            ]
        camera = requirement["camera"] or shot.get("camera") or ""
        shot.update({
            "scene": scene, "visual": visual, "camera": camera,
            "character_keys": [item["character_key"] for item in visible],
            "provider_prompt": short_drama_storyboard._provider_prompt(
                project, visible, scene, visual, camera, str((beats[index] if index < len(beats) else {}).get("phase") or "development"),
            ),
            "source_type": "user_storyboard",
            "source_text": requirement["source_text"],
            "source_offset": requirement["source_offset"],
        })
        if index < len(beats):
            beats[index]["source_fact"] = requirement["source_text"][:180]
            beats[index]["source_type"] = "user_storyboard"
        line = lines[index] if index < len(lines) else None
        if line is not None:
            subtitle = re.search(r"(?:^|[\n；。])\s*字幕\s*[:：]\s*([^\n；。]+)", requirement["source_text"])
            speaker_match = next((
                (item, re.search(
                    r"(?:^|[\n；。])\s*" + re.escape(str(item.get("name") or "")) + r"\s*[:：]\s*([^\n；。]+)",
                    requirement["source_text"],
                )) for item in characters if item.get("name")
            ), None)
            speaker_match = speaker_match if speaker_match and speaker_match[1] else None
            if subtitle:
                line.update({"kind": "on_screen_text", "character_key": "", "speaker": "", "text": subtitle.group(1).strip()})
            elif speaker_match:
                line.update({
                    "kind": "dialogue", "character_key": speaker_match[0]["character_key"],
                    "speaker": speaker_match[0]["name"], "text": speaker_match[1].group(1).strip(),
                })
            elif re.search(r"无台词|静默", requirement["source_text"]):
                line.update({"kind": "silence", "character_key": "", "speaker": "", "text": ""})
            line["estimated_reading_seconds"] = short_drama_storyboard._reading_seconds(line)
    scenes = script.get("scenes") or []
    for index, shot in enumerate(shots):
        if index < len(scenes):
            scenes[index]["location"] = shot.get("scene") or scenes[index].get("location")
            scenes[index]["summary"] = shot.get("visual") or scenes[index].get("summary")
    acts = script.get("acts") or []
    if shots and beats and acts:
        act_indices = (0, max(0, len(shots) // 2), len(shots) - 1)
        for act, index in zip(acts, act_indices):
            if index < len(beats):
                act["summary"] = beats[index].get("source_fact") or act.get("summary")
    script["storyboard_source"] = {
        "mode": "user_priority", "explicit_shot_count": len(by_number),
        "generated_shot_count": len(shots) - len(by_number),
        "unmapped_shot_numbers": [
            item["number"] for item in requirements if item["number"] not in by_number
        ],
    }
    return script


def _scene_location(clause, fallback):
    locations = (
        "房间", "家中", "医院", "学校", "教室", "公园", "车站", "办公室",
        "街道", "天台", "餐厅", "雨夜", "清晨", "深夜",
    )
    return next((value for value in locations if value in clause), fallback)


def _script_v3(project, messages, instruction="", understanding=None):
    understanding = understanding or _understanding(project, messages)
    seed = str(understanding.get("creative_brief") or project["synopsis"]).strip()
    duration = short_drama_duration.choose(
        project["target_duration"], project.get("shot_count") or 0
    )
    shot_count = int(project["shot_count"])
    names = _character_names(seed, understanding)
    if "独角" in seed or "一个人" in seed:
        names = [names[0]]
    characters = [
        {
            "character_key": "character_%d" % (index + 1),
            "name": name,
            "identity": "故事中的%s" % name,
            "personality": "身份、行动和情绪均以已确认故事为准",
        }
        for index, name in enumerate(names)
    ]
    ending = understanding.get("ending") or "有情绪余味的收束"
    clauses = _story_clauses(project, understanding)
    act_clauses = [
        clauses[0],
        clauses[min(len(clauses) - 1, len(clauses) // 2)],
        clauses[-1],
    ]
    acts = [
        {"act": 1, "name": "人物与事件", "summary": act_clauses[0][:180]},
        {"act": 2, "name": "关系与选择", "summary": act_clauses[1][:180]},
        {
            "act": 3,
            "name": "情绪收束",
            "summary": "%s；结尾方向：%s" % (act_clauses[2][:140], ending),
        },
    ]
    scenes = [
        {
            "scene": index + 1,
            "location": _scene_location(clause, "故事场景%d" % (index + 1)),
            "summary": clause[:200],
        }
        for index, clause in enumerate(act_clauses)
    ]
    phase_names = ("建立", "推进", "变化", "冲突", "选择", "收束")
    phase_visuals = (
        "交代人物、时间和关键处境",
        "让人物用动作回应刚发生的事",
        "用具体细节展示情绪变化",
        "把人物关系中的矛盾推到台前",
        "呈现人物必须作出的关键选择",
        "用前文细节完成结局和情绪落点",
    )
    durations = short_drama_duration.allocate(
        project["target_duration"], shot_count, duration,
    )
    shots = []
    dialogue = []
    for index in range(shot_count):
        seconds = durations[index]
        clause = clauses[min(
            len(clauses) - 1,
            index * len(clauses) // max(1, shot_count),
        )]
        phase_index = min(
            len(phase_names) - 1,
            index * len(phase_names) // max(1, shot_count),
        )
        mentioned = [
            item for item in characters if item["name"] in clause
        ]
        visible_characters = mentioned or characters[:min(2, len(characters))]
        quote = re.search(r"[“\"]([^”\"]{1,80})[”\"]", clause)
        speaker = visible_characters[index % len(visible_characters)]
        if quote:
            line = quote.group(1).strip()
        else:
            line = "%s：%s" % (phase_names[phase_index], clause[:70])
        dialogue.append({
            "id": "draft_line_%02d" % (index + 1),
            "character_key": speaker["character_key"],
            "speaker": speaker["name"],
            "text": line,
        })
        shots.append({
            "shot_key": "shot_%02d" % (index + 1),
            "sort_order": index + 1,
            "duration_seconds": seconds,
            "scene": scenes[min(2, index * 3 // max(1, shot_count))]["location"],
            "beat": phase_names[phase_index],
            "visual": "%s；画面严格围绕：%s" % (
                phase_visuals[phase_index], clause[:120]
            ),
            "camera": "中景推进，关键情绪处切近景",
            "character_keys": [
                item["character_key"] for item in visible_characters
            ],
            "dialogue_line_ids": ["draft_line_%02d" % (index + 1)],
        })
    title = project["title"]
    return {
        "schema_version": "short-drama-conversation-script-v3",
        "overview": {
            "title": title,
            "logline": seed[:280],
            "theme": instruction[:160] or "%s、选择与真相" % (understanding.get("tone") or "情绪"),
            "duration_seconds": duration,
            "ratio": project["ratio"],
            "visual_style": project["visual_style"],
        },
        "characters": characters,
        "acts": acts,
        "scenes": scenes,
        "dialogue_lines": dialogue,
        "shots": shots,
    }
def _source_anchors(source):
    source = str(source or "")
    if not source:
        return []

    marker_patterns = (
        re.compile(r"镜头\s*(\d+)\s*[（(][^）)]{1,40}[）)]\s*[：:]?"),
        re.compile(r"(?<!\d)(\d+)\s*[、.]\s*\d+\s*[-—~至]\s*\d+\s*秒\s*[：:]"),
    )
    matches = []
    for pattern in marker_patterns:
        matches.extend(pattern.finditer(source))
    matches.sort(key=lambda item: item.start())
    unique_matches = []
    seen_numbers = set()
    for matched in matches:
        number = int(matched.group(1))
        if number in seen_numbers:
            continue
        seen_numbers.add(number)
        unique_matches.append(matched)

    def compact(value, limit=220):
        value = re.sub(r"\s+", " ", str(value or "")).strip(" ，,。；;：:")
        return value if len(value) <= limit else value[:limit].rstrip() + "…"

    if len(unique_matches) >= 3:
        shots = []
        for index, matched in enumerate(unique_matches):
            end = unique_matches[index + 1].start() if index + 1 < len(unique_matches) else len(source)
            excerpt = compact(source[matched.end():end], 150)
            if excerpt:
                shots.append({"offset": matched.start(), "excerpt": excerpt})
        if len(shots) >= 3:
            boundaries = (0, max(1, len(shots) // 3), max(2, (len(shots) * 2) // 3), len(shots))
            labels = ("start", "middle", "end")
            anchors = []
            for index, label in enumerate(labels):
                group = shots[boundaries[index]:boundaries[index + 1]]
                if not group:
                    continue
                anchors.append({
                    "position": label,
                    "offset": group[0]["offset"],
                    "excerpt": compact("；".join(item["excerpt"] for item in group)),
                })
            if len(anchors) == 3:
                return anchors

    # Very long imports commonly contain dense prose without sentence breaks.
    # Sentence-first truncation can then keep only the beginning of each huge
    # block and silently lose the actual middle/end of the source.  Preserve
    # three bounded positional windows so faithful imports always retain the
    # opening, midpoint and ending evidence.
    if len(source) > 2000:
        width = 320
        positions = (
            0,
            max(0, len(source) // 2 - width // 2),
            max(0, len(source) - width),
        )
        labels = ("start", "middle", "end")
        return [
            {
                "position": label,
                "offset": offset,
                "excerpt": re.sub(r"\s+", " ", source[offset:offset + width]).strip(),
            }
            for label, offset in zip(labels, positions)
        ]

    sentences = [
        compact(item) for item in re.split(r"(?<=[。！？!?])\s*|[\r\n]+", source)
        if compact(item) and not re.match(r"^(人物|角色|场景|时长|分镜数量)\s*[：:]", compact(item))
    ]
    if not sentences:
        return []
    indexes = (0, len(sentences) // 2, len(sentences) - 1)
    labels = ("start", "middle", "end")
    return [
        {"position": label, "offset": source.find(sentences[index][:20]), "excerpt": sentences[index]}
        for label, index in zip(labels, indexes)
    ]


_IMPORT_DIALOGUE_RE = re.compile(
    r"(?m)^\s*([^\s：:，,。！？!?（）()]{1,24})\s*[：:]\s*(.{1,180})\s*$"
)
_IMPORT_DIALOGUE_LABELS = {
    "时间", "地点", "场景", "镜头", "旁白", "画外音", "字幕", "动作",
}


def _all_source_dialogues(source):
    found = []
    for matched in _IMPORT_DIALOGUE_RE.finditer(str(source or "")):
        speaker = matched.group(1).strip()
        dialogue = matched.group(2).strip()
        if speaker in _IMPORT_DIALOGUE_LABELS or not dialogue:
            continue
        found.append({
            "speaker": speaker,
            "text": dialogue,
            "offset": matched.start(2),
        })
    return found


def _source_dialogues(source):
    found = _all_source_dialogues(source)
    if len(found) <= 3:
        return found
    positions = (0, len(found) // 2, len(found) - 1)
    return [found[index] for index in positions]


def _import_global_structure(source, characters):
    source = str(source or "")
    lines = [line.strip() for line in source.splitlines() if line.strip()]
    narrative = [
        line for line in lines
        if not _IMPORT_DIALOGUE_RE.match(line)
        and not re.match(r"^(场景\s*[一二三四五六七八九十\d]*|内景|外景|第.{0,8}[场幕集]|INT\.|EXT\.)", line, re.I)
    ] or lines

    def node(ratio):
        if not narrative:
            return ""
        index = min(len(narrative) - 1, int(len(narrative) * ratio))
        return narrative[index][:240]

    conflict = next((
        line for line in narrative
        if re.search(r"冲突|但是|却|不能|必须|被迫|秘密|真相|误会|选择|失去|阻止", line)
    ), node(.3))
    arcs = []
    for name in characters[:12]:
        evidence = [line for line in lines if name in line]
        arcs.append({
            "character": name,
            "opening": (evidence[0] if evidence else "待确认")[:160],
            "ending": (evidence[-1] if evidence else "待确认")[:160],
            "evidence_count": len(evidence),
        })
    relationships = []
    for index, first in enumerate(characters[:8]):
        for second in characters[index + 1:8]:
            count = sum(1 for line in lines if first in line and second in line)
            if count:
                relationships.append({
                    "characters": [first, second], "evidence_count": count,
                })
    return {
        "schema_version": "short-drama-import-global-v1",
        "premise": node(0),
        "setup": node(.08),
        "development": node(.3),
        "turning_point": node(.55),
        "climax": node(.78),
        "ending": node(.96),
        "central_conflict": str(conflict or "")[:300],
        "character_arcs": arcs,
        "relationships": relationships[:20],
        "coverage": {
            "source_length": len(source),
            "line_count": len(lines),
            "analyzed_from_start": True,
            "analyzed_from_end": True,
        },
    }


_OPTIMIZATION_CHANGES = (
    (
        "structure_pacing", "结构与节奏",
        "允许在不改变核心人物关系和结局的前提下调整场次与节奏。",
        ("结构", "节奏", "场次"),
    ),
    (
        "dialogue_polish", "对白精炼",
        "允许精炼重复对白，但不得改变关键台词表达的事实与人物立场。",
        ("对白", "台词"),
    ),
    (
        "visual_adaptation", "短视频画面化",
        "允许把难以拍摄的叙述转换为可执行镜头，不新增改变剧情的事实。",
        ("画面", "镜头", "画面化"),
    ),
)


def _contract_hash_payload(contract):
    return {
        "source_hash": contract["source_hash"],
        "import_mode": contract["import_mode"],
        "revision": int(contract["revision"]),
        "character_contract_hash": contract.get("character_contract_hash", ""),
        "optimization": [
            {"key": item["key"], "enabled": bool(item.get("enabled"))}
            for item in contract.get("proposed_changes", [])
        ],
        "required_preservations": [
            {
                "id": item["id"], "kind": item["kind"],
                "source_offset": int(item["source_offset"]),
                "source": item["source"],
            }
            for item in contract.get("required_preservations", [])
        ],
        "additional_requirements": list(contract.get("additional_requirements", [])),
    }


def _refresh_contract_hash(contract):
    contract["contract_hash"] = _hash(_contract_hash_payload(contract))
    return contract


def _import_contract(source_import):
    source = str(source_import.get("source_text") or "")
    all_dialogues = _all_source_dialogues(source)
    dialogues = _source_dialogues(source)
    characters = []
    for item in all_dialogues:
        if item["speaker"] not in characters:
            characters.append(item["speaker"])
    stored_characters = source_import.get("character_contract")
    if stored_characters is None:
        stored_characters = _json(
            source_import.get("character_contract_json") or "[]", []
        )
    if isinstance(stored_characters, list) and stored_characters:
        characters = [
            str(item.get("name") or "").strip()
            for item in stored_characters if isinstance(item, dict)
            and str(item.get("name") or "").strip()
        ]
    mode = source_import.get("import_mode")
    changes = []
    if mode == "optimize":
        changes = [{
            "key": key,
            "label": label,
            "summary": summary,
            "importance": "important",
            "enabled": True,
            "status": "pending",
        } for key, label, summary, _aliases in _OPTIMIZATION_CHANGES]
    character_contract_hash = _hash(
        stored_characters if isinstance(stored_characters, list) else []
    )
    global_structure = _import_global_structure(source, characters)
    confirmed_core_story = source_import.get("core_story")
    if confirmed_core_story is None:
        confirmed_core_story = _json(source_import.get("core_story_json") or "{}", {})
    if isinstance(confirmed_core_story, dict) and confirmed_core_story:
        for key in (
                "setup", "development", "turning_point", "climax", "ending",
                "central_conflict"):
            if str(confirmed_core_story.get(key) or "").strip():
                global_structure[key] = str(confirmed_core_story[key]).strip()
        global_structure["premise"] = str(
            confirmed_core_story.get("logline") or global_structure.get("premise") or ""
        ).strip()
        global_structure["confirmed_core_story"] = dict(confirmed_core_story)
    contract = {
        "source_hash": source_import["source_hash"],
        "import_mode": mode,
        "revision": 1,
        "source_length": len(source),
        "characters": characters,
        "character_contract": stored_characters if isinstance(stored_characters, list) else [],
        "character_contract_hash": character_contract_hash,
        "content_type": str(source_import.get("content_type") or "live_action"),
        "global_structure": global_structure,
        "plot_points": _source_anchors(source),
        "key_dialogues": dialogues,
        "proposed_changes": changes,
        "required_preservations": [],
        "additional_requirements": [],
    }
    return _refresh_contract_hash(contract)


def _optimization_change_actions(content, contract):
    actions = []
    only_match = re.search(r"(?:只|仅)(?:允许|优化|调整|修改)?([^。！？!?；;]{0,80})", content)
    only_keys = set()
    if only_match:
        only_text = only_match.group(1)
        for key, _label, _summary, aliases in _OPTIMIZATION_CHANGES:
            if any(alias in only_text for alias in aliases):
                only_keys.add(key)
        if only_keys:
            for item in contract.get("proposed_changes", []):
                actions.append({
                    "action": "set_optimization",
                    "key": item["key"],
                    "enabled": item["key"] in only_keys,
                })
    deny_words = ("不要", "禁止", "不允许", "不得", "别", "取消")
    allow_words = ("允许", "可以", "恢复", "保留优化", "继续优化")
    for key, _label, _summary, aliases in _OPTIMIZATION_CHANGES:
        positions = [content.find(alias) for alias in aliases if alias in content]
        if not positions:
            continue
        position = min(value for value in positions if value >= 0)
        clause_start = max(
            [content.rfind(mark, 0, position) for mark in "，,。！？!?；;"] + [-1]
        ) + 1
        clause_ends = [
            content.find(mark, position) for mark in "，,。！？!?；;"
            if content.find(mark, position) >= 0
        ]
        clause_end = min(clause_ends) if clause_ends else len(content)
        window = content[clause_start:clause_end]
        if any(word in window for word in deny_words):
            actions.append({
                "action": "set_optimization", "key": key, "enabled": False,
            })
        elif any(word in window for word in allow_words):
            actions.append({
                "action": "set_optimization", "key": key, "enabled": True,
            })
    deduplicated = {}
    for action in actions:
        deduplicated[action["key"]] = action
    return list(deduplicated.values())


def _faithful_preservation_action(source, content):
    if not re.search(r"(?:必须|务必|一定要|需要)保留|保留原稿", content):
        return None
    quoted = re.findall(r"[“\"]([^”\"]{1,180})[”\"]", content)
    fragment = quoted[0].strip() if quoted else ""
    if not fragment:
        matched = re.search(
            r"(?:必须|务必|一定要|需要)保留(?:的)?(?:原稿)?(?:对白|台词|剧情|节点)?"
            r"\s*[：:]\s*(.{1,180})$",
            content,
        )
        fragment = matched.group(1).strip() if matched else ""
    if not fragment:
        return None
    offset = source.find(fragment)
    if offset < 0:
        return {
            "action": "add_requirement",
            "text": "待核对必保内容：" + fragment,
        }
    line_start = source.rfind("\n", 0, offset) + 1
    line_end = source.find("\n", offset)
    if line_end < 0:
        line_end = len(source)
    source_line = source[line_start:line_end].strip()
    dialogue_match = _IMPORT_DIALOGUE_RE.match(source_line)
    kind = "dialogue" if dialogue_match or re.search(r"对白|台词", content) else "plot_point"
    item = {
        "id": _hash({"kind": kind, "offset": offset, "source": fragment})[:16],
        "kind": kind,
        "source_offset": offset,
        "source": fragment,
    }
    if dialogue_match:
        item["speaker"] = dialogue_match.group(1).strip()
        item["text"] = dialogue_match.group(2).strip()
    elif kind == "dialogue":
        item["speaker"] = "原稿人物"
        item["text"] = fragment
    return {"action": "add_preservation", "item": item}


def _parse_import_contract_changes(source_import, content, contract):
    if source_import["import_mode"] == "optimize":
        actions = _optimization_change_actions(content, contract)
    else:
        action = _faithful_preservation_action(
            str(source_import.get("source_text") or ""), content,
        )
        actions = [action] if action else []
    if (not actions and not _IMPORT_PURE_CONFIRM_RE.fullmatch(content.strip())
            and _REVISION_RE.search(content)):
        actions.append({"action": "add_requirement", "text": content[:500]})
    return actions


def _is_pure_import_confirmation(content, changes):
    value = str(content or "").strip()
    return bool(
        not changes
        and not _QUESTION_RE.search(value)
        and _IMPORT_PURE_CONFIRM_RE.fullmatch(value)
    )


def _apply_import_contract_changes(contract, actions):
    changed = False
    for action in actions or []:
        if action.get("action") == "set_optimization":
            target = next((
                item for item in contract.get("proposed_changes", [])
                if item["key"] == action.get("key")
            ), None)
            enabled = bool(action.get("enabled"))
            if target is not None and bool(target.get("enabled")) != enabled:
                target["enabled"] = enabled
                changed = True
        elif action.get("action") == "add_preservation":
            item = action.get("item") or {}
            if item.get("id") and not any(
                existing.get("id") == item["id"]
                for existing in contract.get("required_preservations", [])
            ):
                contract.setdefault("required_preservations", []).append(item)
                changed = True
        elif action.get("action") == "add_requirement":
            value = str(action.get("text") or "").strip()
            if value and value not in contract.get("additional_requirements", []):
                contract.setdefault("additional_requirements", []).append(value)
                changed = True
    if changed:
        contract["revision"] = int(contract.get("revision") or 1) + 1
        for item in contract.get("proposed_changes", []):
            item["status"] = "pending" if item.get("enabled") else "denied"
        _refresh_contract_hash(contract)
    return changed


def _import_understanding(project, source_import, messages):
    contract = _import_contract(source_import)
    user_items = [
        item for item in messages
        if item.get("role") == "user" and str(item.get("content") or "").strip()
    ]
    user_messages = [str(item.get("content") or "").strip() for item in user_items]
    non_confirmation_messages = [
        str(item.get("content") or "").strip()
        for item in user_items
        if (item.get("metadata") or {}).get("kind") != "import_confirmation"
    ]
    confirmed = False
    invalidated = False
    for index, item in enumerate(user_items):
        value = str(item.get("content") or "").strip()
        metadata = item.get("metadata") or {}
        if metadata.get("kind") == "import_contract_change":
            change_matches = (
                metadata.get("source_hash") == source_import["source_hash"]
                and metadata.get("import_mode") == source_import["import_mode"]
                and metadata.get("base_contract_revision") == contract["revision"]
                and metadata.get("base_contract_hash") == contract["contract_hash"]
            )
            if change_matches and _apply_import_contract_changes(
                contract, metadata.get("changes") or [],
            ):
                invalidated = invalidated or confirmed
                confirmed = False
        elif _CONFIRM_RE.match(value):
            if (
                metadata.get("kind") == "import_confirmation"
                and metadata.get("source_hash") == source_import["source_hash"]
                and metadata.get("import_mode") == source_import["import_mode"]
                and metadata.get("contract_revision") == contract["revision"]
                and metadata.get("contract_hash") == contract["contract_hash"]
            ):
                confirmed = True
                invalidated = False
    if confirmed:
        contract["confirmed_source_hash"] = source_import["source_hash"]
        contract["confirmed_import_mode"] = source_import["import_mode"]
        contract["confirmed_contract_revision"] = contract["revision"]
        contract["confirmed_contract_hash"] = contract["contract_hash"]
        for item in contract["proposed_changes"]:
            item["status"] = "confirmed" if item.get("enabled") else "denied"
    mode_name = "尊重原稿" if source_import["import_mode"] == "faithful" else "AI 协助优化"
    question = (
        "请确认人物、剧情节点和关键对白保留清单，确认后才能生成首版剧本。"
        if source_import["import_mode"] == "faithful" else
        "请确认拟优化的结构、对白与画面化边界；重要改动确认后才会进入生成。"
    )
    if invalidated:
        question = "优化或保留要求已经变化，请检查新的理解快照并重新确认。"
    summary = question
    if confirmed:
        summary = (
            "人物、剧情节点和关键对白保留清单已经确认。"
            if source_import["import_mode"] == "faithful" else
            "结构、对白和画面化优化边界已经确认。"
        )
    return {
        "premise": project["synopsis"],
        "creative_brief": project["synopsis"],
        "audience": "短视频观众",
        "tone": project["visual_style"],
        "ending": "",
        "protagonist": contract["characters"][0] if contract["characters"] else "",
        "platform": project["target_platform"],
        "duration_seconds": short_drama_duration.choose(
            project["target_duration"], project.get("shot_count") or 0
        ),
        "duration_range_seconds": list(
            short_drama_duration.bounds(project["target_duration"])
        ),
        "ratio": project["ratio"],
        "latest_request": user_messages[-1][-1200:] if user_messages else "",
        "story_notes": non_confirmation_messages[-8:],
        "user_requirements": {
            "story_notes": non_confirmation_messages[-8:],
            "import_mode": source_import["import_mode"],
        },
        "selected_direction": {
            "id": "import_" + source_import["import_mode"],
            "title": mode_name,
            "summary": summary,
        },
        "phase": "direction_ready" if confirmed else "import_review",
        "missing_fields": [],
        "recommendations": [],
        "selected_recommendation_id": "import_" + source_import["import_mode"],
        "direction_confirmed": confirmed,
        "confirmation_invalidated": invalidated,
        "ready_to_generate": confirmed,
        "open_questions": [] if confirmed else [question],
        "import_contract": contract,
    }


def _import_assistant_reply(source_import, understanding):
    contract = understanding["import_contract"]
    mode = source_import["import_mode"]
    if understanding["direction_confirmed"]:
        return (
            (
                "原稿理解与处理边界（契约第 %d 版）已确认。后续生成会锁定当前原稿哈希、"
                "处理模式和契约哈希；原稿或模式变化后必须重新确认。"
            ) % contract["revision"],
            {
                "kind": "import_understanding_confirmed",
                "phase": "direction_ready",
                "quick_replies": ["补充必须保留的对白", "调整结局要求"],
                "import_contract": contract,
            },
        )
    if understanding.get("confirmation_invalidated"):
        return (
            "我已记录新的处理要求，之前的原稿理解确认已失效。"
            "请检查更新后的保留或优化边界，再重新确认。",
            {
                "kind": "import_understanding_invalidated",
                "phase": "import_review",
                "quick_replies": ["确认调整后的原稿理解"],
                "import_contract": contract,
            },
        )
    character_text = "、".join(contract["characters"][:6]) or "待人工确认"
    if mode == "faithful":
        content = (
            "我已完整读取原稿并建立全局理解快照，覆盖开场、发展、关键转折、高潮和结局。识别人物：%s；已提取首、中、尾剧情节点"
            "以及 %d 条关键对白。选择“尊重原稿”后，这些内容会形成可追溯的保留映射。"
            "请核对后确认，确认前不会生成剧本。"
            % (character_text, len(contract["key_dialogues"]))
        )
        replies = ["确认尊重原稿并生成", "补充必须保留的对白"]
    else:
        content = (
            "我已完整读取原稿并建立全局理解快照，覆盖开场、发展、关键转折、高潮和结局。识别人物：%s；拟优化结构节奏、重复对白和"
            "画面化表达，不改变核心人物关系与结局。以上属于重要改动边界，请确认后再生成。"
            % character_text
        )
        replies = ["确认优化范围", "调整优化范围"]
    return content, {
        "kind": "import_understanding",
        "phase": "import_review",
        "quick_replies": replies,
        "import_contract": contract,
    }


def seed_import_conversation(conn, owner_username, project_id):
    """Seed the imported-script understanding inside the import transaction."""
    project = _project(conn, owner_username, project_id)
    source_import = _import_snapshot(conn, project_id)
    if not source_import:
        raise ConversationError("script_import_missing", "导入原稿快照不存在", 409)
    now = int(time.time())
    understanding = _import_understanding(project, source_import, [])
    reply, metadata = _import_assistant_reply(source_import, understanding)
    conn.execute(
        "INSERT INTO short_drama_conversations "
        "(project_id,state,understanding_json,revision,created_at,updated_at) "
        "VALUES (?,'direction_review',?,1,?,?)",
        (project_id, _json_text(understanding), now, now),
    )
    conn.execute(
        "INSERT INTO short_drama_conversation_messages "
        "(id,project_id,role,content,metadata_json,created_at) VALUES (?,?,?,?,?,?)",
        (
            str(uuid.uuid4()), project_id, "assistant", reply,
            _json_text(metadata), int(time.time() * 1000),
        ),
    )


def _hydrate_import_conversation(conn, project):
    """Backfill the review snapshot for imports created before this contract."""
    source_import = _import_snapshot(conn, project["id"])
    if not source_import:
        return
    current = _conversation(conn, project["id"])
    understanding = _json(current.get("understanding_json"), {})
    contract = understanding.get("import_contract") or {}
    expected_contract = _import_contract(source_import)
    if (
        contract.get("source_hash") == source_import["source_hash"]
        and contract.get("import_mode") == source_import["import_mode"]
        and contract.get("character_contract_hash")
        == expected_contract["character_contract_hash"]
    ):
        return
    messages = _messages(conn, project["id"])
    understanding = _import_understanding(project, source_import, messages)
    reply, metadata = _import_assistant_reply(source_import, understanding)
    now = int(time.time())
    conn.execute(
        "UPDATE short_drama_conversations SET state='direction_review',"
        "understanding_json=?,updated_at=? WHERE project_id=?",
        (_json_text(understanding), now, project["id"]),
    )
    conn.execute(
        "INSERT INTO short_drama_conversation_messages "
        "(id,project_id,role,content,metadata_json,created_at) VALUES (?,?,?,?,?,?)",
        (
            str(uuid.uuid4()), project["id"], "assistant", reply,
            _json_text(metadata), int(time.time() * 1000),
        ),
    )


def _apply_faithful_contract(script, contract):
    preservation = []
    shots = script.get("shots") or []
    beats = script.get("story_beats") or []
    lines = script.get("dialogue_lines") or []
    characters = script.get("characters") or []
    for name in contract.get("characters") or []:
        target = next(
            (item for item in characters if item.get("name") == name), None
        )
        if target:
            preservation.append({
                "kind": "character", "source": name,
                "target": "characters.%s" % target.get("character_key"),
            })
    anchors = contract.get("plot_points") or []
    for index, anchor in enumerate(anchors):
        if not beats:
            break
        target_index = int(round(index * (len(beats) - 1) / max(1, len(anchors) - 1)))
        excerpt = str(anchor.get("excerpt") or "").strip()
        beats[target_index]["source_fact"] = excerpt[:180]
        if target_index < len(shots):
            shots[target_index]["visual"] = (
                shots[target_index]["visual"].split("；剧情事实：", 1)[0]
                + "；原稿事实：" + excerpt[:120]
            )
        preservation.append({
            "kind": "plot_point", "position": anchor.get("position"),
            "source_offset": anchor.get("offset"),
            "target": "story_beats.%s.source_fact" % beats[target_index]["beat_key"],
        })
    dialogues = contract.get("key_dialogues") or []
    for index, dialogue in enumerate(dialogues):
        if not lines:
            break
        target_index = int(round(index * (len(lines) - 1) / max(1, len(dialogues) - 1)))
        line = lines[target_index]
        character = next(
            (item for item in characters if item.get("name") == dialogue["speaker"]),
            characters[0] if characters else {},
        )
        line.update({
            "kind": "dialogue",
            "character_key": character.get("character_key", ""),
            "speaker": dialogue["speaker"],
            "text": dialogue["text"],
            "estimated_reading_seconds": short_drama_storyboard._reading_seconds({
                "kind": "dialogue", "text": dialogue["text"],
            }),
        })
        preservation.append({
            "kind": "dialogue", "source_offset": dialogue["offset"],
            "source": "%s：%s" % (dialogue["speaker"], dialogue["text"]),
            "target": "dialogue_lines.%s" % line["id"],
        })
    source_length = max(1, int(contract.get("source_length") or 1))
    for requirement in contract.get("required_preservations") or []:
        existing = next((
            item for item in preservation
            if item.get("kind") == requirement.get("kind")
            and item.get("source_offset") == requirement.get("source_offset")
        ), None)
        if existing:
            existing["requirement_id"] = requirement["id"]
            existing["source"] = requirement["source"]
            continue
        if requirement.get("kind") == "dialogue" and lines:
            target_index = min(
                len(lines) - 1,
                int(int(requirement["source_offset"]) * len(lines) / source_length),
            )
            line = lines[target_index]
            speaker = requirement.get("speaker") or "原稿人物"
            character = next(
                (item for item in characters if item.get("name") == speaker),
                characters[0] if characters else {},
            )
            text = requirement.get("text") or requirement["source"]
            line.update({
                "kind": "dialogue",
                "character_key": character.get("character_key", ""),
                "speaker": speaker,
                "text": text,
                "estimated_reading_seconds": short_drama_storyboard._reading_seconds({
                    "kind": "dialogue", "text": text,
                }),
            })
            preservation.append({
                "kind": "dialogue",
                "requirement_id": requirement["id"],
                "source_offset": requirement["source_offset"],
                "source": requirement["source"],
                "target": "dialogue_lines.%s" % line["id"],
            })
        elif beats:
            target_index = min(
                len(beats) - 1,
                int(int(requirement["source_offset"]) * len(beats) / source_length),
            )
            beats[target_index]["source_fact"] = requirement["source"][:180]
            preservation.append({
                "kind": requirement.get("kind") or "plot_point",
                "requirement_id": requirement["id"],
                "source_offset": requirement["source_offset"],
                "source": requirement["source"],
                "target": "story_beats.%s.source_fact" % beats[target_index]["beat_key"],
            })
    return preservation


def _script(project, messages, instruction="", understanding=None, source_import=None):
    """Build the editable v4 story-beat contract.

    The previous v3 template builder remains above for reading historical
    snapshots and targeted rollback, but all new versions use the concrete
    storyboard compiler.
    """
    understanding = understanding or _understanding(project, messages)
    seed = str(
        (source_import or {}).get("source_text")
        or understanding.get("creative_brief")
        or project.get("synopsis") or ""
    ).strip()
    names = _character_names(seed, understanding)
    import_contract = None
    if source_import:
        candidate = understanding.get("import_contract") or {}
        if (
            candidate.get("source_hash") == source_import["source_hash"]
            and candidate.get("import_mode") == source_import["import_mode"]
        ):
            import_contract = candidate
        else:
            import_contract = _import_contract(source_import)
    if import_contract and import_contract.get("characters"):
        names = list(dict.fromkeys(import_contract["characters"]))[:8]
    if "独角" in seed or "一个人" in seed:
        names = names[:1]
    role_contract = (
        import_contract.get("character_contract") or []
        if import_contract else []
    )
    if role_contract:
        characters = [{
            "character_key": str(
                item.get("character_key") or "character_%d" % (index + 1)
            ),
            "name": str(item.get("name") or names[index]),
            "identity": str(
                item.get("identity_text") or "故事中的%s" % names[index]
            ),
            "personality": str(
                item.get("personality")
                or "身份、行动和情绪均以已确认故事为准"
            ),
            "role_type": str(item.get("role_type") or "support"),
        } for index, item in enumerate(role_contract[:8])]
    else:
        characters = [
            {
                "character_key": "character_%d" % (index + 1),
                "name": name,
                "identity": "故事中的%s" % name,
                "personality": "身份、行动和情绪均以已确认故事为准",
                "role_type": "main" if index == 0 else "support",
            }
            for index, name in enumerate(names)
        ]
    clauses = _story_clauses(project, understanding, source_import)
    script = short_drama_storyboard.compile_storyboard(
        project,
        clauses,
        characters,
        instruction=instruction,
        ending=understanding.get("ending") or "",
        understanding=understanding,
    )
    if source_import:
        if source_import["import_mode"] == "faithful":
            preservation = _apply_faithful_contract(script, import_contract)
            script["preservation_map"] = preservation
            script["import_behavior"] = "faithful_preservation"
        else:
            changes = [
                dict(item, status="confirmed")
                for item in import_contract["proposed_changes"]
                if item.get("enabled") and item.get("status") == "confirmed"
            ]
            script["optimization_plan"] = {
                "status": "confirmed",
                "changes": changes,
                "excluded_changes": [
                    dict(item) for item in import_contract["proposed_changes"]
                    if not item.get("enabled")
                ],
            }
            script["import_behavior"] = "confirmed_optimization"
        script["source_import"] = {
            "source_hash": source_import["source_hash"],
            "import_mode": source_import["import_mode"],
            "character_count": len(source_import["source_text"]),
            "anchors": _source_anchors(source_import["source_text"]),
            "contract_revision": import_contract["revision"],
            "contract_hash": import_contract["contract_hash"],
        }
        script = _apply_explicit_storyboard(
            script,
            project,
            source_import.get("source_text") or "",
        )
    else:
        script = _apply_explicit_storyboard(script, project, "")
    short_drama_storyboard.normalize_dialogue_timing(script)
    return script


def _validate_script(script):
    quality = short_drama_storyboard.validate_script(script)
    storyboard_source = script.get("storyboard_source") or {}
    unmapped = storyboard_source.get("unmapped_shot_numbers") or []
    if unmapped:
        quality.setdefault("warnings", []).append({
            "code": "explicit_storyboard_shot_count_mismatch",
            "message": "完整剧本中的分镜 %s 超出项目镜头数量，未写入当前版本" % "、".join(
                str(value) for value in unmapped
            ),
        })
        if quality.get("status") == "pass":
            quality["status"] = "warning"
    script["quality_gate"] = quality
    if quality["status"] == "blocked":
        blocker = quality["blockers"][0]
        raise ConversationError(
            blocker.get("code") or "script_quality_blocked",
            blocker.get("message") or "剧本质量门禁未通过",
            422,
        )


def _readable(script):
    lines = [
        script["overview"]["title"],
        "一句话故事：" + script["overview"]["logline"],
        "",
    ]
    for act in script["acts"]:
        lines.append("第%d幕·%s：%s" % (act["act"], act["name"], act["summary"]))
    lines.append("")
    for line in script["dialogue_lines"]:
        if line.get("kind") == "silence":
            lines.append("（静默表演）")
        elif line.get("kind") == "on_screen_text":
            lines.append("画面文字：%s" % line.get("text", ""))
        else:
            lines.append("%s：%s" % (line.get("speaker", ""), line.get("text", "")))
    return "\n".join(lines)


def _messages(conn, project_id):
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id,role,content,metadata_json,created_at "
        "FROM short_drama_conversation_messages WHERE project_id=? "
        "ORDER BY created_at,rowid",
        (project_id,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "role": row["role"],
            "content": row["content"],
            "metadata": _json(row["metadata_json"], {}),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def _import_snapshot(conn, project_id):
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT source_text,source_hash,filename,content_type,character_contract_json,"
        "core_story_json,core_story_confirmed_at,import_mode,status,created_at "
        "FROM short_drama_script_imports WHERE project_id=? AND status='completed'",
        (project_id,),
    ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["character_contract"] = _json(
        result.pop("character_contract_json", "[]"), []
    )
    result["core_story"] = _json(result.pop("core_story_json", "{}"), {})
    return result


def _versions(conn, project_id):
    conn.row_factory = sqlite3.Row
    return [
        _snapshot(row)
        for row in conn.execute(
            "SELECT * FROM short_drama_script_snapshots WHERE project_id=? "
            "ORDER BY version DESC",
            (project_id,),
        ).fetchall()
    ]


def _workspace_locked(conn, project_id):
    current = _conversation(conn, project_id)
    current_version = (
        _snapshot_by_id(conn, project_id, current["current_version_id"])
        if current.get("current_version_id")
        else None
    )
    return current, current_version


def _workspace(conn, project, actor_username, can_edit=True):
    current, current_version = _workspace_locked(conn, project["id"])
    source_import = _import_snapshot(conn, project["id"])
    return {
        "project": project,
        "conversation": {
            "state": current["state"],
            "revision": int(current["revision"]),
            "understanding": _json(current["understanding_json"], {}),
            "current_version_id": current["current_version_id"],
            "locked_version_id": current["locked_version_id"],
        },
        "messages": _messages(conn, project["id"]),
        "current_script": current_version,
        "versions": _versions(conn, project["id"]),
        "permissions": {"can_edit": bool(can_edit), "actor": actor_username},
        "billing": {"cost": 0, "charged": False},
        "script_import": ({
            "status": source_import["status"],
            "source_hash": source_import["source_hash"],
            "filename": source_import["filename"],
            "import_mode": source_import["import_mode"],
            "character_count": len(source_import["source_text"]),
        } if source_import else None),
    }


def workspace(db_factory, owner_username, actor_username, project_id, can_edit=True):
    conn = _connection(db_factory)
    try:
        project = _project(conn, owner_username, project_id)
        _ensure_conversation(conn, project_id)
        _hydrate_import_conversation(conn, project)
        conn.commit()
        return _workspace(conn, project, actor_username, can_edit)
    finally:
        conn.close()


def get_job(db_factory, owner_username, project_id, job_id):
    conn = _connection(db_factory)
    conn.row_factory = sqlite3.Row
    try:
        _project(conn, owner_username, project_id)
        row = conn.execute(
            "SELECT id,project_id,kind,status,result_version_id,error_json,"
            "created_at,updated_at FROM short_drama_conversation_jobs "
            "WHERE id=? AND project_id=?",
            (job_id, project_id),
        ).fetchone()
        if not row:
            raise LookupError("conversation job does not exist")
        result = dict(row)
        result["error"] = _json(result.pop("error_json"), None)
        return result
    finally:
        conn.close()


def _existing_request(conn, actor, project_id, operation, key, request_hash):
    row = conn.execute(
        "SELECT request_hash,response_json FROM short_drama_conversation_requests "
        "WHERE actor_username=? AND project_id=? AND operation=? AND idempotency_key=?",
        (actor, project_id, operation, key),
    ).fetchone()
    if not row:
        return None
    if row[0] != request_hash:
        raise ConversationError(
            "idempotency_conflict", "该幂等键已用于不同请求", 409
        )
    return _json(row[1], {})


def _store_request(conn, actor, project_id, operation, key, request_hash, response):
    conn.execute(
        "INSERT INTO short_drama_conversation_requests "
        "(id,actor_username,project_id,operation,idempotency_key,request_hash,"
        "response_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            str(uuid.uuid4()),
            actor,
            project_id,
            operation,
            key,
            request_hash,
            _json_text(response),
            int(time.time()),
        ),
    )


def send_message(db_factory, owner_username, actor_username, body, idempotency_key):
    project_id = str(body.get("project_id") or "").strip()
    content = _message(body.get("message"))
    revision = _request_revision(body)
    key = _idempotency_key(idempotency_key)
    request_hash = _hash({"revision": revision, "message": content})
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        project = _project(conn, owner_username, project_id)
        _ensure_conversation(conn, project_id)
        _hydrate_import_conversation(conn, project)
        replay = _existing_request(conn, actor_username, project_id, "message", key, request_hash)
        if replay is not None:
            conn.rollback()
            replay["replayed"] = True
            return replay
        current = _conversation(conn, project_id)
        if int(current["revision"]) != revision:
            raise ConversationError("conversation_revision_conflict", "对话已更新，请刷新后重试", 409)
        if current["state"] == "script_locked":
            raise ConversationError("script_locked", "剧本已经锁定，不能继续修改", 409)
        now = int(time.time())
        message_now = int(time.time() * 1000)
        source_import = _import_snapshot(conn, project_id)
        message_metadata = {}
        if source_import:
            contract = (_json(current.get("understanding_json"), {}).get(
                "import_contract"
            ) or _import_contract(source_import))
            changes = _parse_import_contract_changes(
                source_import, content, contract,
            )
            if changes:
                message_metadata = {
                    "kind": "import_contract_change",
                    "source_hash": source_import["source_hash"],
                    "import_mode": source_import["import_mode"],
                    "base_contract_revision": contract["revision"],
                    "base_contract_hash": contract["contract_hash"],
                    "changes": changes,
                }
            elif _is_pure_import_confirmation(content, changes):
                message_metadata = {
                    "kind": "import_confirmation",
                    "source_hash": source_import["source_hash"],
                    "import_mode": source_import["import_mode"],
                    "contract_revision": contract["revision"],
                    "contract_hash": contract["contract_hash"],
                }
        conn.execute(
            "INSERT INTO short_drama_conversation_messages "
            "(id,project_id,role,content,metadata_json,created_at) VALUES (?,?,?,?,?,?)",
            (
                str(uuid.uuid4()), project_id, "user", content,
                _json_text(message_metadata), message_now,
            ),
        )
        messages = _messages(conn, project_id)
        if source_import:
            understanding = _import_understanding(project, source_import, messages)
            reply, reply_metadata = _import_assistant_reply(
                source_import, understanding
            )
        else:
            understanding = _understanding(project, messages)
            reply, reply_metadata = _assistant_reply(project, understanding)
        conn.execute(
            "INSERT INTO short_drama_conversation_messages "
            "(id,project_id,role,content,metadata_json,created_at) VALUES (?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                project_id,
                "assistant",
                reply,
                _json_text(reply_metadata),
                message_now + 1,
            ),
        )
        next_state = "direction_review"
        conn.execute(
            "UPDATE short_drama_conversations SET state=?,understanding_json=?,"
            "revision=revision+1,updated_at=? WHERE project_id=? AND revision=?",
            (next_state, _json_text(understanding), now, project_id, revision),
        )
        response = _workspace(conn, project, actor_username)
        response["replayed"] = False
        _store_request(conn, actor_username, project_id, "message", key, request_hash, response)
        conn.commit()
        return response
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _confirmed_contract_text(value, field, limit, required=True):
    text = str(value or "").strip()
    if required and not text:
        raise ConversationError("confirmed_contract_invalid", "%s 不能为空" % field, 422)
    if len(text) > limit:
        raise ConversationError("confirmed_contract_invalid", "%s 内容过长" % field, 422)
    return text


def _normalize_confirmed_contract(project, value):
    if not isinstance(value, dict):
        raise ConversationError("confirmed_contract_invalid", "确认剧本合同格式无效", 422)
    if value.get("schema_version") != "preproject-confirmed-shot-contract-v1":
        raise ConversationError("confirmed_contract_invalid", "确认剧本合同版本不受支持", 422)
    characters = []
    for item in value.get("characters") or []:
        name = _confirmed_contract_text(item, "characters", 40)
        if name not in characters:
            characters.append(name)
    if not characters or len(characters) > 8:
        raise ConversationError("confirmed_contract_invalid", "确认剧本角色数量无效", 422)
    shot_count = int(value.get("shot_count") or 0)
    duration_seconds = int(value.get("duration_seconds") or 0)
    if shot_count != int(project.get("shot_count") or 0):
        raise ConversationError("confirmed_contract_project_mismatch", "确认镜头数与项目不一致", 409)
    if not short_drama_duration.contains(
            project.get("target_duration"), duration_seconds):
        raise ConversationError(
            "confirmed_contract_project_mismatch", "确认时长不在项目所选区间内", 409
        )
    if str(value.get("ratio") or "") != str(project.get("ratio") or ""):
        raise ConversationError("confirmed_contract_project_mismatch", "确认画幅与项目不一致", 409)
    raw_shots = value.get("shots") or []
    if not isinstance(raw_shots, list) or len(raw_shots) != shot_count:
        raise ConversationError("confirmed_contract_invalid", "确认镜头列表不完整", 422)
    shots = []
    allowed_kinds = {"dialogue", "voiceover", "on_screen_text", "silence"}
    for position, raw in enumerate(raw_shots, 1):
        if not isinstance(raw, dict) or int(raw.get("index") or 0) != position:
            raise ConversationError("confirmed_contract_invalid", "确认镜头顺序无效", 422)
        visible = []
        for name in raw.get("characters") or []:
            name = _confirmed_contract_text(name, "shot.characters", 40)
            if name not in characters:
                raise ConversationError("confirmed_contract_invalid", "镜头包含未知角色", 422)
            if name not in visible:
                visible.append(name)
        kind = _confirmed_contract_text(raw.get("dialogue_kind") or "silence", "dialogue_kind", 30)
        if kind not in allowed_kinds:
            raise ConversationError("confirmed_contract_invalid", "镜头台词类型无效", 422)
        speaker = _confirmed_contract_text(raw.get("speaker"), "speaker", 40, required=False)
        dialogue = _confirmed_contract_text(raw.get("dialogue"), "dialogue", 120, required=False)
        if kind in {"dialogue", "voiceover"} and (speaker not in characters or not dialogue):
            raise ConversationError("confirmed_contract_invalid", "镜头说话角色或台词无效", 422)
        if kind == "silence":
            speaker, dialogue = "", ""
        shots.append({
            "index": position,
            "phase": _confirmed_contract_text(raw.get("phase"), "phase", 40),
            "duration": int(raw.get("duration") or 0),
            "scene": _confirmed_contract_text(raw.get("scene"), "scene", 80),
            "characters": visible,
            "action": _confirmed_contract_text(raw.get("action"), "action", 360),
            "expression": _confirmed_contract_text(raw.get("expression"), "expression", 180),
            "speaker": speaker,
            "dialogue_kind": kind,
            "dialogue": dialogue,
            "camera": _confirmed_contract_text(raw.get("camera"), "camera", 180),
            "sound": _confirmed_contract_text(raw.get("sound"), "sound", 220),
            "transition": _confirmed_contract_text(raw.get("transition"), "transition", 120),
            "continuity": _confirmed_contract_text(raw.get("continuity"), "continuity", 220),
            "summary": _confirmed_contract_text(raw.get("summary"), "summary", 220),
            "locked": bool(raw.get("locked")),
        })
    if any(item["duration"] < 1 for item in shots) or sum(item["duration"] for item in shots) != duration_seconds:
        raise ConversationError("confirmed_contract_invalid", "确认镜头总时长无效", 422)
    beats = []
    for position, raw in enumerate(value.get("beats") or [], 1):
        if not isinstance(raw, dict):
            raise ConversationError("confirmed_contract_invalid", "确认节拍格式无效", 422)
        beats.append({
            "index": int(raw.get("index") or position),
            "phase": _confirmed_contract_text(raw.get("phase"), "beat.phase", 40),
            "summary": _confirmed_contract_text(raw.get("summary"), "beat.summary", 220),
            "duration": int(raw.get("duration") or 0),
        })
    if beats and len(beats) != shot_count:
        raise ConversationError("confirmed_contract_invalid", "确认节拍列表不完整", 422)
    normalized = {
        "schema_version": "preproject-confirmed-shot-contract-v1",
        "title": _confirmed_contract_text(value.get("title"), "title", 120),
        "logline": _confirmed_contract_text(value.get("logline"), "logline", 2000),
        "protagonist": _confirmed_contract_text(value.get("protagonist"), "protagonist", 80),
        "conflict": _confirmed_contract_text(value.get("conflict"), "conflict", 500),
        "ending": _confirmed_contract_text(value.get("ending"), "ending", 220),
        "ratio": str(value.get("ratio") or ""),
        "duration_seconds": duration_seconds,
        "shot_count": shot_count,
        "visual_style": _confirmed_contract_text(value.get("visual_style"), "visual_style", 120),
        "characters": characters,
        "beats": beats,
        "shots": shots,
    }
    raw_memory = value.get("creative_memory")
    if isinstance(raw_memory, dict):
        if raw_memory.get("schema_version") != "short-drama-creative-memory-v1":
            raise ConversationError("confirmed_contract_invalid", "创作记忆版本不受支持", 422)
        raw_fields = raw_memory.get("fields") or {}
        if not isinstance(raw_fields, dict):
            raise ConversationError("confirmed_contract_invalid", "创作记忆格式无效", 422)
        normalized["creative_memory"] = {
            "schema_version": "short-drama-creative-memory-v1",
            "fields": {
                key: _confirmed_contract_text(
                    raw_fields.get(key), "creative_memory.%s" % key, 500, required=False
                )
                for key in ("topic", "protagonist", "conflict", "emotion", "ending", "audience", "style")
            },
        }
    raw_plan = value.get("story_plan")
    if isinstance(raw_plan, dict):
        if raw_plan.get("schema_version") != "short-drama-story-plan-v1":
            raise ConversationError("confirmed_contract_invalid", "故事策划版本不受支持", 422)
        plan = {"schema_version": "short-drama-story-plan-v1"}
        for key in (
            "premise", "theme", "audience", "emotion", "dramatic_question",
            "character_goal", "obstacle", "stakes", "hook", "turning_point",
            "climax", "resolution",
        ):
            plan[key] = _confirmed_contract_text(
                raw_plan.get(key), "story_plan.%s" % key, 500,
            )
        acts = []
        for position, raw in enumerate(raw_plan.get("acts") or [], 1):
            if not isinstance(raw, dict) or int(raw.get("act") or 0) != position:
                raise ConversationError("confirmed_contract_invalid", "故事幕结构无效", 422)
            acts.append({
                "act": position,
                "name": _confirmed_contract_text(raw.get("name"), "story_plan.act.name", 80),
                "purpose": _confirmed_contract_text(raw.get("purpose"), "story_plan.act.purpose", 300),
                "summary": _confirmed_contract_text(raw.get("summary"), "story_plan.act.summary", 500),
            })
        if len(acts) != 3:
            raise ConversationError("confirmed_contract_invalid", "故事策划必须包含三幕", 422)
        plan["acts"] = acts
        normalized["story_plan"] = plan
    raw_scenes = value.get("scenes")
    if isinstance(raw_scenes, list):
        scenes = []
        last_end = 0
        for position, raw in enumerate(raw_scenes, 1):
            if not isinstance(raw, dict) or int(raw.get("index") or 0) != position:
                raise ConversationError("confirmed_contract_invalid", "分场顺序无效", 422)
            start, end = int(raw.get("shot_start") or 0), int(raw.get("shot_end") or 0)
            if start != last_end + 1 or end < start or end > shot_count:
                raise ConversationError("confirmed_contract_invalid", "分场镜头范围无效", 422)
            scene_characters = []
            for name in raw.get("characters") or []:
                name = _confirmed_contract_text(name, "scene.characters", 40)
                if name not in characters:
                    raise ConversationError("confirmed_contract_invalid", "分场包含未知角色", 422)
                if name not in scene_characters:
                    scene_characters.append(name)
            scenes.append({
                "index": position,
                "phase": _confirmed_contract_text(raw.get("phase"), "scene.phase", 80),
                "location": _confirmed_contract_text(raw.get("location"), "scene.location", 120),
                "characters": scene_characters,
                "objective": _confirmed_contract_text(raw.get("objective"), "scene.objective", 400),
                "conflict": _confirmed_contract_text(raw.get("conflict"), "scene.conflict", 500),
                "turn": _confirmed_contract_text(raw.get("turn"), "scene.turn", 500),
                "shot_start": start,
                "shot_end": end,
            })
            last_end = end
        if not scenes or last_end != shot_count:
            raise ConversationError("confirmed_contract_invalid", "分场没有覆盖全部镜头", 422)
        normalized["scenes"] = scenes
    raw_review = value.get("script_review")
    if isinstance(raw_review, dict):
        if raw_review.get("schema_version") != "short-drama-script-review-v1":
            raise ConversationError("confirmed_contract_invalid", "剧本审稿版本不受支持", 422)
        status = _confirmed_contract_text(raw_review.get("status"), "script_review.status", 30)
        if status not in {"passed", "needs_revision", "blocked"}:
            raise ConversationError("confirmed_contract_invalid", "剧本审稿状态无效", 422)
        issues = []
        for raw in (raw_review.get("issues") or [])[:50]:
            if not isinstance(raw, dict):
                continue
            issues.append({
                "severity": _confirmed_contract_text(raw.get("severity"), "script_review.severity", 30),
                "scope": _confirmed_contract_text(raw.get("scope"), "script_review.scope", 30),
                "index": int(raw.get("index") or 0),
                "code": _confirmed_contract_text(raw.get("code"), "script_review.code", 80),
                "message": _confirmed_contract_text(raw.get("message"), "script_review.message", 300),
                "repairable": bool(raw.get("repairable")),
            })
        normalized["script_review"] = {
            "schema_version": "short-drama-script-review-v1",
            "score": max(0, min(100, int(raw_review.get("score") or 0))),
            "status": status,
            "issues": issues,
        }
    return normalized


def _script_from_confirmed_contract(project, value, instruction):
    contract = _normalize_confirmed_contract(project, value)
    characters = [
        {
            "character_key": "character_%d" % (index + 1),
            "name": name,
            "identity": "用户确认逐镜合同中的%s" % name,
            "personality": "以用户确认的动作、表情、台词和连续性为准",
        }
        for index, name in enumerate(contract["characters"])
    ]
    character_keys = {item["name"]: item["character_key"] for item in characters}
    script = short_drama_storyboard.compile_storyboard(
        dict(project, title=contract["title"], synopsis=contract["logline"]),
        [contract["logline"]],
        characters,
        instruction=instruction,
        ending=contract["ending"],
        understanding={"creative_brief": contract["logline"]},
    )
    script["overview"].update({
        "title": contract["title"], "logline": contract["logline"],
        "theme": contract["conflict"], "duration_seconds": contract["duration_seconds"],
        "ratio": contract["ratio"], "visual_style": contract["visual_style"],
    })
    for index, confirmed in enumerate(contract["shots"]):
        shot = script["shots"][index]
        line = script["dialogue_lines"][index]
        beat = script["story_beats"][index]
        line.update({
            "kind": confirmed["dialogue_kind"],
            "character_key": character_keys.get(confirmed["speaker"], ""),
            "speaker": confirmed["speaker"], "text": confirmed["dialogue"],
        })
        line["estimated_reading_seconds"] = short_drama_storyboard._reading_seconds(line)
        shot.update({
            "duration_seconds": confirmed["duration"], "scene": confirmed["scene"],
            "beat": confirmed["phase"], "purpose": confirmed["summary"],
            "visual": "%s；表情：%s；确认镜头：%d" % (
                confirmed["action"], confirmed["expression"], confirmed["index"]
            ),
            "action": confirmed["action"], "expression": confirmed["expression"],
            "camera": confirmed["camera"], "sound": confirmed["sound"],
            "transition": confirmed["transition"], "continuity": confirmed["continuity"],
            "character_keys": [character_keys[name] for name in confirmed["characters"]],
            "provider_prompt": "%s；场景：%s；动作：%s；表情：%s；镜头：%s；连续性：%s" % (
                contract["visual_style"], confirmed["scene"], confirmed["action"],
                confirmed["expression"], confirmed["camera"], confirmed["continuity"],
            ),
            "locked": confirmed["locked"],
        })
        beat.update({
            "phase": confirmed["phase"], "label": confirmed["phase"],
            "purpose": confirmed["summary"], "source_fact": confirmed["summary"],
            "action": confirmed["action"],
        })
        script["scenes"][index].update({
            "location": confirmed["scene"], "summary": confirmed["action"],
        })
    script["confirmed_contract"] = contract
    script["confirmed_contract_hash"] = _hash(contract)
    short_drama_storyboard.normalize_dialogue_timing(script)
    _validate_script(script)
    return script


def _create_version(
    conn, project, actor, current, instruction, parent_id=None,
    confirmed_contract=None,
):
    messages = _messages(conn, project["id"])
    source_import = _import_snapshot(conn, project["id"])
    understanding = _json(current.get("understanding_json"), {})
    if not understanding:
        understanding = _understanding(project, messages)
    script = (
        _script_from_confirmed_contract(project, confirmed_contract, instruction)
        if confirmed_contract is not None
        else _script(
            project, messages, instruction, understanding,
            source_import=source_import,
        )
    )
    _validate_script(script)
    version = int(conn.execute(
        "SELECT COALESCE(MAX(version),0)+1 FROM short_drama_script_snapshots "
        "WHERE project_id=?",
        (project["id"],),
    ).fetchone()[0])
    if version > MAX_VERSIONS:
        raise ConversationError("script_version_limit", "剧本版本数量已达上限")
    version_id = str(uuid.uuid4())
    now = int(time.time())
    conn.execute(
        "INSERT INTO short_drama_script_snapshots "
        "(id,project_id,version,parent_id,status,script_json,readable_text,input_hash,"
        "provider,model_version,instruction,change_summary,created_by,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            version_id,
            project["id"],
            version,
            parent_id,
            "draft",
            _json_text(script),
            _readable(script),
            _hash({
                "project": project, "messages": messages,
                "instruction": instruction,
                "source_import": ({
                    "source_hash": source_import["source_hash"],
                    "import_mode": source_import["import_mode"],
                    "contract_revision": (
                        understanding.get("import_contract") or {}
                    ).get("revision"),
                    "contract_hash": (
                        understanding.get("import_contract") or {}
                    ).get("contract_hash"),
                } if source_import else None),
            }),
            (
                "preproject-confirmed-contract"
                if confirmed_contract is not None
                else "creative-advisor-local"
            ),
            (
                "preproject-confirmed-shot-contract-v1"
                if confirmed_contract is not None
                else short_drama_storyboard.MODEL_VERSION
            ),
            instruction,
            (
                "固化用户确认的完整逐镜合同"
                if confirmed_contract is not None
                else ("根据修改要求生成新版本" if parent_id else "根据对话生成首版结构化剧本")
            ),
            actor,
            now,
        ),
    )
    conn.execute(
        "UPDATE short_drama_conversations SET state='script_review',current_version_id=?,"
        "revision=revision+1,updated_at=? WHERE project_id=? AND revision=?",
        (version_id, now, project["id"], int(current["revision"])),
    )
    return version_id


def generate_script(db_factory, owner_username, actor_username, body, idempotency_key):
    project_id = str(body.get("project_id") or "").strip()
    revision = _request_revision(body)
    instruction = str(body.get("instruction") or "").strip()[:2000]
    confirmed_contract = body.get("confirmed_contract")
    key = _idempotency_key(idempotency_key)
    request_hash = _hash({
        "revision": revision,
        "instruction": instruction,
        "confirmed_contract": confirmed_contract,
    })
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        project = _project(conn, owner_username, project_id)
        _ensure_conversation(conn, project_id)
        _hydrate_import_conversation(conn, project)
        replay = _existing_request(conn, actor_username, project_id, "generate", key, request_hash)
        if replay is not None:
            conn.rollback()
            replay["replayed"] = True
            return replay
        current = _conversation(conn, project_id)
        if int(current["revision"]) != revision:
            raise ConversationError("conversation_revision_conflict", "对话已更新，请刷新后重试", 409)
        if current["state"] == "script_locked":
            raise ConversationError("script_locked", "剧本已经锁定，不能重新生成", 409)
        understanding = _json(current.get("understanding_json"), {})
        source_import = _import_snapshot(conn, project_id)
        import_contract = understanding.get("import_contract") or {}
        import_confirmation_stale = bool(source_import) and (
            import_contract.get("confirmed_source_hash") != source_import["source_hash"]
            or import_contract.get("confirmed_import_mode") != source_import["import_mode"]
            or import_contract.get("confirmed_contract_revision")
            != import_contract.get("revision")
            or import_contract.get("confirmed_contract_hash")
            != import_contract.get("contract_hash")
        )
        confirmation_required = (
            bool(source_import) and (
                not understanding.get("direction_confirmed")
                or import_confirmation_stale
            )
        ) or (
            not source_import
            and not current["current_version_id"]
            and not understanding.get("direction_confirmed")
        )
        if confirmation_required:
            raise ConversationError(
                "direction_confirmation_required",
                "请先和创作助手确认创作方向，再生成首版剧本",
                409,
            )
        job_id = str(uuid.uuid4())
        now = int(time.time())
        conn.execute(
            "INSERT INTO short_drama_conversation_jobs "
            "(id,project_id,actor_username,kind,status,request_json,created_at,updated_at) "
            "VALUES (?,?,?,'script_generate','running',?,?,?)",
            (
                job_id,
                project_id,
                actor_username,
                _json_text({"instruction": instruction, "conversation_revision": revision}),
                now,
                now,
            ),
        )
        version_id = _create_version(
            conn,
            project,
            actor_username,
            current,
            instruction,
            current["current_version_id"],
            confirmed_contract=confirmed_contract,
        )
        conn.execute(
            "UPDATE short_drama_conversation_jobs SET status='succeeded',"
            "result_version_id=?,updated_at=? WHERE id=?",
            (version_id, int(time.time()), job_id),
        )
        response = _workspace(conn, project, actor_username)
        response.update({
            "job": {"id": job_id, "status": "succeeded", "result_version_id": version_id},
            "replayed": False,
        })
        _store_request(conn, actor_username, project_id, "generate", key, request_hash, response)
        conn.commit()
        return response
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_SHOT_EDIT_FIELDS = {
    "scene": 80,
    "purpose": 160,
    "visual": 360,
    "camera": 180,
    "continuity": 220,
    "provider_prompt": 1200,
    "negative_prompt": 500,
}
_DIALOGUE_KINDS = {"dialogue", "voiceover", "on_screen_text", "silence"}
_SPEECH_RATES = {1.0, 1.15, 1.3, 1.5, 2.0}


def _current_editable_version(conn, project_id, current, version_id):
    if current["state"] == "script_locked":
        raise ConversationError("script_locked", "剧本已经锁定，不能编辑镜头", 409)
    if current["current_version_id"] != version_id:
        raise ConversationError("stale_script_version", "只能编辑当前剧本版本", 409)
    version = _snapshot_by_id(conn, project_id, version_id)
    if not version:
        raise LookupError("script version does not exist")
    return version


def _shot_and_line(script, shot_key):
    shots = script.get("shots") or []
    shot = next(
        (item for item in shots if str(item.get("shot_key")) == shot_key),
        None,
    )
    if not shot:
        raise ConversationError("shot_not_found", "镜头不存在", 404)
    line_ids = list(shot.get("dialogue_line_ids") or [])
    line = next(
        (
            item
            for item in script.get("dialogue_lines") or []
            if str(item.get("id")) in line_ids
        ),
        None,
    )
    if not line:
        raise ConversationError("shot_dialogue_missing", "镜头台词结构不完整", 422)
    return shot, line


def _rebalance_duration(script, edited_shot, requested_seconds):
    requested_seconds = int(requested_seconds)
    if requested_seconds < 4 or requested_seconds > 15:
        raise ConversationError("shot_duration_invalid", "镜头时长必须为 4 至 15 秒", 422)
    edited_shot["duration_seconds"] = requested_seconds
    (script.setdefault("overview", {}))["duration_seconds"] = sum(
        int(item.get("duration_seconds") or 0)
        for item in script.get("shots") or []
    )


def _refresh_shot_structure(script):
    shots = list(script.get("shots") or [])
    for index, shot in enumerate(shots, 1):
        shot["sort_order"] = index
    (script.setdefault("overview", {}))["duration_seconds"] = sum(
        int(item.get("duration_seconds") or 0) for item in shots
    )
    script["shot_planning"] = {
        "mode": "user_adjustable",
        "shot_count": len(shots),
        "duration_seconds": script["overview"]["duration_seconds"],
    }


def _new_structure_key(script, prefix, collection, field):
    existing = {str(item.get(field) or "") for item in script.get(collection) or []}
    while True:
        value = "%s_%s" % (prefix, uuid.uuid4().hex[:10])
        if value not in existing:
            return value


def _structure_shot(script, shot_key, action, instruction=""):
    shots = script.get("shots") or []
    neighbor_offsets = {
        "delete": (-1, 1),
        "copy": (1,),
        "insert_before": (-1,),
        "insert_after": (1,),
        "smart_insert": (1,),
        "move_up": (-2, -1, 1),
        "move_down": (-1, 1, 2),
    }
    if action not in neighbor_offsets:
        raise ConversationError(
            "shot_structure_action_invalid", "不支持的镜头调整操作", 422,
        )
    if action == "delete" and len(shots) <= 1:
        raise ConversationError("last_shot_required", "至少保留一个镜头", 422)
    shot, line = _shot_and_line(script, shot_key)
    index = shots.index(shot)
    affected = [shot]
    for offset in neighbor_offsets[action]:
        neighbor_index = index + offset
        if 0 <= neighbor_index < len(shots):
            affected.append(shots[neighbor_index])
    if any(bool(item.get("locked")) for item in affected):
        raise ConversationError(
            "shot_locked", "请先解锁受本次结构调整影响的镜头", 409,
        )
    if action == "delete":
        shots.pop(index)
        line_ids = {str(value) for value in shot.get("dialogue_line_ids") or []}
        script["dialogue_lines"] = [
            item for item in script.get("dialogue_lines") or []
            if str(item.get("id")) not in line_ids
        ]
    elif action in {"copy", "insert_before", "insert_after", "smart_insert"}:
        clone = _json(_json_text(shot), {})
        clone_line = _json(_json_text(line), {})
        clone["shot_key"] = _new_structure_key(script, "shot_user", "shots", "shot_key")
        clone_line["id"] = _new_structure_key(script, "line_user", "dialogue_lines", "id")
        clone["dialogue_line_ids"] = [clone_line["id"]]
        clone["locked"] = False
        insert_at = index if action == "insert_before" else index + 1
        if action == "copy":
            clone["source_type"] = "user_copy"
            clone_line["source_type"] = "user_copy"
            clone["purpose"] = "延续并补充：%s" % str(shot.get("purpose") or "剧情推进")[:140]
            clone["visual"] = "延续上一镜头后的新动作：%s" % str(shot.get("visual") or "人物继续行动")[:300]
            clone["continuity"] = "紧接上一镜头，保持人物、场景、服装和关键道具一致"
            clone["provider_prompt"] = "%s。作为新的连续镜头，不重复上一镜头构图。" % clone["visual"]
        else:
            neighbor_index = index - 1 if action == "insert_before" else index + 1
            neighbor = shots[neighbor_index] if 0 <= neighbor_index < len(shots) else shot
            clone["purpose"] = instruction[:160] or "承接相邻镜头的过渡与剧情推进"
            clone["visual"] = instruction[:360] or "承接%s，并自然过渡到%s" % (
                str(shot.get("visual") or "当前动作")[:120],
                str(neighbor.get("visual") or "下一段剧情")[:120],
            )
            clone["continuity"] = "继承相邻镜头的时间、场景、人物位置、服装和关键道具"
            clone["provider_prompt"] = "%s。保持前后镜头人物、场景、光线和动作连续。" % clone["visual"]
            clone_line.update({"kind": "silence", "character_key": "", "speaker": "", "text": "", "estimated_reading_seconds": 0.0})
        shots.insert(insert_at, clone)
        script.setdefault("dialogue_lines", []).append(clone_line)
    elif action in {"move_up", "move_down"}:
        target = index - 1 if action == "move_up" else index + 1
        if 0 <= target < len(shots):
            shots[index], shots[target] = shots[target], shots[index]
    else:
        raise ConversationError("shot_structure_action_invalid", "不支持的镜头调整操作", 422)
    _refresh_shot_structure(script)
    _validate_script(script)
    return script


def _apply_shot_patch(script, shot_key, changes):
    shot, line = _shot_and_line(script, shot_key)
    if bool(shot.get("locked")):
        raise ConversationError("shot_locked", "请先解锁当前镜头再修改", 409)
    for field, limit in _SHOT_EDIT_FIELDS.items():
        if field not in changes:
            continue
        value = str(changes.get(field) or "").strip()
        if field in {"visual", "purpose", "provider_prompt"} and not value:
            raise ConversationError("shot_field_required", "%s 不能为空" % field, 422)
        if len(value) > limit:
            raise ConversationError("shot_field_too_long", "%s 内容过长" % field, 422)
        shot[field] = value
    if "duration_seconds" in changes:
        _rebalance_duration(script, shot, changes["duration_seconds"])
    dialogue = changes.get("dialogue")
    if dialogue is not None:
        if not isinstance(dialogue, dict):
            raise ConversationError("dialogue_invalid", "台词修改格式无效", 422)
        kind = str(dialogue.get("kind") or line.get("kind") or "dialogue")
        if kind not in _DIALOGUE_KINDS:
            raise ConversationError("dialogue_kind_invalid", "台词类型无效", 422)
        value = str(dialogue.get("text") or "").strip()
        if kind != "silence" and not value:
            raise ConversationError("dialogue_text_required", "非静默镜头必须填写内容", 422)
        if len(value) > 120:
            raise ConversationError("dialogue_too_long", "单镜头台词不能超过 120 字", 422)
        character_key = str(dialogue.get("character_key") or "").strip()
        speaker = str(dialogue.get("speaker") or "").strip()
        try:
            speech_rate = float(
                dialogue.get("speech_rate") or line.get("speech_rate") or 1.0
            )
        except (TypeError, ValueError):
            speech_rate = 1.0
        if speech_rate not in _SPEECH_RATES:
            raise ConversationError("speech_rate_invalid", "请选择有效的语速", 422)
        if kind in {"dialogue", "voiceover"}:
            character = next(
                (
                    item
                    for item in script.get("characters") or []
                    if item.get("character_key") == character_key
                ),
                None,
            )
            if not character:
                raise ConversationError("speaker_unknown", "请选择剧本中的有效角色", 422)
            speaker = character["name"]
        elif kind == "on_screen_text":
            character_key = ""
            speaker = "画面文字"
        else:
            character_key = ""
            speaker = ""
            value = ""
        line.update({
            "kind": kind,
            "character_key": character_key,
            "speaker": speaker,
            "text": value,
            "speech_rate": (
                speech_rate if kind in {"dialogue", "voiceover"} else 1.0
            ),
        })
        line["estimated_reading_seconds"] = short_drama_storyboard._reading_seconds(line)
    beat = next(
        (
            item
            for item in script.get("story_beats") or []
            if item.get("beat_key") == shot.get("beat_key")
        ),
        None,
    )
    if beat:
        beat["purpose"] = shot.get("purpose") or beat.get("purpose")
        beat["action"] = shot.get("visual") or beat.get("action")
    _validate_script(script)
    return script


def _regenerate_user_shot(script, shot, line, instruction):
    """Regenerate an inserted shot from its real timeline context."""
    shots = script.get("shots") or []
    shot_index = shots.index(shot)
    previous_shot = shots[shot_index - 1] if shot_index > 0 else None
    next_shot = shots[shot_index + 1] if shot_index + 1 < len(shots) else None

    def context(item, fallback):
        if not item:
            return fallback
        return str(item.get("visual") or item.get("purpose") or fallback).strip()[:80]

    previous_context = context(previous_shot, "本段开场状态")
    current_context = context(shot, "当前剧情动作")
    next_context = context(next_shot, "本段收束状态")
    request = instruction or "重新组织当前动作和构图"
    visual = (
        "承接上一镜头“%s”；保留当前剧情事实“%s”；按要求“%s”重新设计动作与构图；"
        "并自然过渡到下一镜头“%s”"
        % (previous_context, current_context, request[:100], next_context)
    )[:_SHOT_EDIT_FIELDS["visual"]]
    continuity = (
        "前接“%s”，后接“%s”；保持人物位置、服装、场景光线和关键道具连续"
        % (previous_context[:60], next_context[:60])
    )[:_SHOT_EDIT_FIELDS["continuity"]]

    replacement = _json(_json_text(shot), {})
    replacement_line = _json(_json_text(line), {})
    replacement.update({
        "purpose": (instruction[:160] or str(shot.get("purpose") or "推进相邻镜头间的剧情"))[:160],
        "visual": visual,
        "continuity": continuity,
        "provider_prompt": (
            "场景：%s。当前镜头：%s。摄影：%s。%s"
            % (
                str(shot.get("scene") or "沿用当前场景")[:80],
                visual,
                str(shot.get("camera") or "沿用当前机位")[:180],
                continuity,
            )
        )[:_SHOT_EDIT_FIELDS["provider_prompt"]],
        "locked": False,
    })
    return replacement, replacement_line


def _insert_edited_version(conn, project, actor, current, source, script, instruction, summary):
    version_number = int(conn.execute(
        "SELECT COALESCE(MAX(version),0)+1 FROM short_drama_script_snapshots "
        "WHERE project_id=?",
        (project["id"],),
    ).fetchone()[0])
    if version_number > MAX_VERSIONS:
        raise ConversationError("script_version_limit", "剧本版本数量已达上限")
    version_id = str(uuid.uuid4())
    now = int(time.time())
    conn.execute(
        "INSERT INTO short_drama_script_snapshots "
        "(id,project_id,version,parent_id,status,script_json,readable_text,input_hash,"
        "provider,model_version,instruction,change_summary,created_by,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            version_id,
            project["id"],
            version_number,
            source["id"],
            "draft",
            _json_text(script),
            _readable(script),
            _hash({
                "parent_input_hash": source.get("input_hash"),
                "script": script,
                "instruction": instruction,
            }),
            "creative-advisor-local",
            short_drama_storyboard.MODEL_VERSION,
            instruction[:2000],
            summary[:220],
            actor,
            now,
        ),
    )
    conn.execute(
        "UPDATE short_drama_conversations SET state='script_review',current_version_id=?,"
        "revision=revision+1,updated_at=? WHERE project_id=? AND revision=?",
        (version_id, now, project["id"], int(current["revision"])),
    )
    return version_id


def _mutate_shot(
    db_factory,
    owner_username,
    actor_username,
    body,
    idempotency_key,
    operation,
):
    project_id = str(body.get("project_id") or "").strip()
    revision = _request_revision(body)
    version_id = str(body.get("version_id") or "").strip()
    shot_key = str(body.get("shot_key") or "").strip()
    key = _idempotency_key(idempotency_key)
    request_hash = _hash(body)
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        project = _project(conn, owner_username, project_id)
        _ensure_conversation(conn, project_id)
        replay = _existing_request(
            conn, actor_username, project_id, operation, key, request_hash
        )
        if replay is not None:
            conn.rollback()
            replay["replayed"] = True
            return replay
        current = _conversation(conn, project_id)
        if int(current["revision"]) != revision:
            raise ConversationError(
                "conversation_revision_conflict",
                "剧本已被其他操作更新，请刷新后重试",
                409,
            )
        source = _current_editable_version(conn, project_id, current, version_id)
        script = _json(_json_text(source["script"]), {})
        if operation == "shot_update":
            changes = body.get("changes")
            if not isinstance(changes, dict) or not changes:
                raise ConversationError("shot_changes_required", "请提交镜头修改内容", 422)
            _apply_shot_patch(script, shot_key, changes)
            instruction = str(body.get("instruction") or "人工编辑镜头").strip()
            summary = "人工编辑 %s" % shot_key
        elif operation == "shot_regenerate":
            shot, _line = _shot_and_line(script, shot_key)
            if bool(shot.get("locked")):
                raise ConversationError("shot_locked", "请先解锁当前镜头再重新生成", 409)
            instruction = str(body.get("instruction") or "").strip()[:500]
            shot_index = script["shots"].index(shot)
            generated = None
            if shot_key.startswith("shot_user_"):
                replacement, replacement_line = _regenerate_user_shot(
                    script, shot, _line, instruction,
                )
            else:
                messages = _messages(conn, project_id)
                understanding = _json(current.get("understanding_json"), {})
                generated = _script(project, messages, instruction, understanding)
                replacement, replacement_line = _shot_and_line(generated, shot_key)
                replacement = _json(_json_text(replacement), {})
                replacement_line = _json(_json_text(replacement_line), {})
            replacement["shot_key"] = shot_key
            replacement["dialogue_line_ids"] = list(shot.get("dialogue_line_ids") or [])
            replacement["duration_seconds"] = shot["duration_seconds"]
            replacement["sort_order"] = shot.get("sort_order")
            replacement["beat_key"] = shot.get("beat_key")
            replacement["source_type"] = shot.get("source_type") or replacement.get("source_type")
            replacement["locked"] = False
            replacement_line["id"] = _line["id"]
            replacement_line["source_type"] = (
                _line.get("source_type") or replacement_line.get("source_type")
            )
            if instruction and not shot_key.startswith("shot_user_"):
                replacement["purpose"] = instruction[:160]
                replacement["visual"] = "%s；调整要求：%s" % (
                    replacement["visual"],
                    instruction[:160],
                )
                replacement["provider_prompt"] = "%s 用户补充要求：%s。" % (
                    replacement["provider_prompt"],
                    instruction[:300],
                )
            script["shots"][shot_index] = replacement
            target_line = _shot_and_line(script, shot_key)[1]
            line_index = script["dialogue_lines"].index(target_line)
            script["dialogue_lines"][line_index] = replacement_line
            beat_index = next(
                (
                    index
                    for index, item in enumerate(script.get("story_beats") or [])
                    if item.get("beat_key") == replacement.get("beat_key")
                ),
                None,
            )
            if beat_index is not None and generated is not None:
                script["story_beats"][beat_index] = generated["story_beats"][beat_index]
            elif beat_index is not None:
                script["story_beats"][beat_index]["purpose"] = replacement.get("purpose")
                script["story_beats"][beat_index]["action"] = replacement.get("visual")
            _validate_script(script)
            summary = "重新生成 %s" % shot_key
        else:
            shot, _line = _shot_and_line(script, shot_key)
            shot["locked"] = bool(body.get("locked"))
            _validate_script(script)
            instruction = "锁定镜头" if shot["locked"] else "解锁镜头"
            summary = "%s %s" % (instruction, shot_key)
        _insert_edited_version(
            conn,
            project,
            actor_username,
            current,
            source,
            script,
            instruction,
            summary,
        )
        response = _workspace(conn, project, actor_username)
        response["replayed"] = False
        _store_request(
            conn, actor_username, project_id, operation, key, request_hash, response
        )
        conn.commit()
        return response
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_shot(db_factory, owner_username, actor_username, body, idempotency_key):
    return _mutate_shot(
        db_factory,
        owner_username,
        actor_username,
        body,
        idempotency_key,
        "shot_update",
    )


def regenerate_shot(db_factory, owner_username, actor_username, body, idempotency_key):
    return _mutate_shot(
        db_factory,
        owner_username,
        actor_username,
        body,
        idempotency_key,
        "shot_regenerate",
    )


def set_shot_lock(db_factory, owner_username, actor_username, body, idempotency_key):
    return _mutate_shot(
        db_factory,
        owner_username,
        actor_username,
        body,
        idempotency_key,
        "shot_lock",
    )


def change_shot_structure(db_factory, owner_username, actor_username, body, idempotency_key):
    project_id = str(body.get("project_id") or "").strip()
    revision = _request_revision(body)
    version_id = str(body.get("version_id") or "").strip()
    shot_key = str(body.get("shot_key") or "").strip()
    action = str(body.get("action") or "").strip()
    instruction = str(body.get("instruction") or "").strip()
    key = _idempotency_key(idempotency_key)
    request_hash = _hash(body)
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        project = _project(conn, owner_username, project_id)
        _ensure_conversation(conn, project_id)
        replay = _existing_request(conn, actor_username, project_id, "shot_structure", key, request_hash)
        if replay is not None:
            conn.rollback()
            replay["replayed"] = True
            return replay
        current = _conversation(conn, project_id)
        if int(current["revision"]) != revision:
            raise ConversationError("conversation_revision_conflict", "剧本已更新，请刷新后重试", 409)
        if current["state"] == "script_locked":
            raise ConversationError("script_locked", "剧本已经锁定，不能调整镜头结构", 409)
        if current["current_version_id"] != version_id:
            raise ConversationError("stale_script_version", "只能调整当前剧本版本", 409)
        source = _snapshot_by_id(conn, project_id, version_id)
        if not source:
            raise LookupError("script version does not exist")
        script = _json(_json_text(source["script"]), {})
        _structure_shot(script, shot_key, action, instruction)
        _insert_edited_version(
            conn, project, actor_username, current, source, script,
            "调整镜头结构：%s" % action,
            "镜头结构已调整，旧合成版本需要重新生成",
        )
        response = _workspace(conn, project, actor_username)
        response["replayed"] = False
        response["assembly_invalidated"] = True
        _store_request(conn, actor_username, project_id, "shot_structure", key, request_hash, response)
        conn.commit()
        return response
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def restore_version(db_factory, owner_username, actor_username, body, idempotency_key):
    project_id = str(body.get("project_id") or "").strip()
    revision = _request_revision(body)
    source_id = str(body.get("version_id") or "").strip()
    key = _idempotency_key(idempotency_key)
    request_hash = _hash({"revision": revision, "version_id": source_id})
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        project = _project(conn, owner_username, project_id)
        _ensure_conversation(conn, project_id)
        replay = _existing_request(conn, actor_username, project_id, "restore", key, request_hash)
        if replay is not None:
            conn.rollback()
            replay["replayed"] = True
            return replay
        current = _conversation(conn, project_id)
        if int(current["revision"]) != revision:
            raise ConversationError("conversation_revision_conflict", "对话已更新，请刷新后重试", 409)
        if current["state"] == "script_locked":
            raise ConversationError("script_locked", "剧本已经锁定，不能恢复历史版本", 409)
        source = _snapshot_by_id(conn, project_id, source_id)
        if not source:
            raise LookupError("script version does not exist")
        version = int(conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM short_drama_script_snapshots WHERE project_id=?",
            (project_id,),
        ).fetchone()[0])
        version_id = str(uuid.uuid4())
        now = int(time.time())
        conn.execute(
            "INSERT INTO short_drama_script_snapshots "
            "(id,project_id,version,parent_id,status,script_json,readable_text,input_hash,"
            "provider,model_version,instruction,change_summary,created_by,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                version_id,
                project_id,
                version,
                source_id,
                "draft",
                _json_text(source["script"]),
                source["readable_text"],
                source["input_hash"],
                source["provider"],
                source["model_version"],
                "恢复历史版本",
                "从 v%d 恢复为新版本" % source["version"],
                actor_username,
                now,
            ),
        )
        conn.execute(
            "UPDATE short_drama_conversations SET state='script_review',current_version_id=?,"
            "revision=revision+1,updated_at=? WHERE project_id=? AND revision=?",
            (version_id, now, project_id, revision),
        )
        response = _workspace(conn, project, actor_username)
        response["replayed"] = False
        _store_request(conn, actor_username, project_id, "restore", key, request_hash, response)
        conn.commit()
        return response
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def lock_script(db_factory, owner_username, actor_username, body, idempotency_key):
    project_id = str(body.get("project_id") or "").strip()
    revision = _request_revision(body)
    version_id = str(body.get("version_id") or "").strip()
    key = _idempotency_key(idempotency_key)
    request_hash = _hash({"revision": revision, "version_id": version_id})
    conn = _connection(db_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        project = _project(conn, owner_username, project_id)
        _ensure_conversation(conn, project_id)
        replay = _existing_request(conn, actor_username, project_id, "lock", key, request_hash)
        if replay is not None:
            conn.rollback()
            replay["replayed"] = True
            return replay
        current = _conversation(conn, project_id)
        if int(current["revision"]) != revision:
            raise ConversationError("conversation_revision_conflict", "对话已更新，请刷新后重试", 409)
        if current["state"] == "script_locked":
            if current["locked_version_id"] == version_id:
                response = _workspace(conn, project, actor_username)
                response["replayed"] = True
                conn.rollback()
                return response
            raise ConversationError("script_locked", "项目已有锁定剧本", 409)
        version = _snapshot_by_id(conn, project_id, version_id)
        if not version:
            raise LookupError("script version does not exist")
        if current["current_version_id"] != version_id:
            raise ConversationError("stale_script_version", "只能锁定当前剧本版本", 409)
        _validate_script(version["script"])
        now = int(time.time())
        conn.execute(
            "UPDATE short_drama_script_snapshots SET status='superseded' "
            "WHERE project_id=? AND id<>? AND status='draft'",
            (project_id, version_id),
        )
        conn.execute(
            "UPDATE short_drama_script_snapshots SET status='locked',locked_by=?,locked_at=? "
            "WHERE project_id=? AND id=?",
            (actor_username, now, project_id, version_id),
        )
        conn.execute(
            "UPDATE short_drama_conversations SET state='script_locked',locked_version_id=?,"
            "revision=revision+1,updated_at=? WHERE project_id=? AND revision=?",
            (version_id, now, project_id, revision),
        )
        response = _workspace(conn, project, actor_username)
        response["replayed"] = False
        _store_request(conn, actor_username, project_id, "lock", key, request_hash, response)
        conn.commit()
        return response
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
