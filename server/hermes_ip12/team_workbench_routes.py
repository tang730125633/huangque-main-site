from flask import request, jsonify, render_template, Response, send_from_directory
from pathlib import Path
from datetime import datetime
from werkzeug.utils import secure_filename
import csv
import io
import json
import mimetypes
import uuid
from artifact_store import atomic_append_bytes, atomic_write_bytes, reserve_capacity


VALID_TYPES = {
    "content_submit": "内容提交",
    "daily_summary": "今日总结",
    "real_media": "真实图片/视频素材",
    "real_copy": "真实文案素材",
    "image_test": "AI图片测试",
    "video_test": "AI视频测试",
    "copy_score": "文案评分",
    "pain_point": "美业痛点",
    "competitor": "竞品收集",
    "feedback": "问题反馈",
    "daily_video_report": "每日视频复盘",
}

ALLOWED_UPLOAD_EXTS = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".mp4", ".mov", ".webm", ".m4v",
    ".txt", ".md", ".pdf", ".doc", ".docx"
}

TODAY_TASKS = [
    {"group": "员工每日流程", "task": "上午交内容：文案/图片/视频/封面/截图统一提交到【内容提交】。", "submit_type": "content_submit", "standard": "只填一次；系统/老板后面再评分和入库，不要在多个入口重复交。"},
    {"group": "员工每日流程", "task": "下午/晚上填【视频数据】：一行一条视频，按平台、账号、链接、播放、互动、私信记录。", "submit_type": "video_data_table", "standard": "只记录客观数据，用来统计哪个平台、账号、视频有效。"},
    {"group": "员工每日流程", "task": "收工前写【今日总结】：只回答三句话，今天最好、最大问题、明天改什么。", "submit_type": "daily_summary", "standard": "不再重复填链接和播放数据，数据交给视频数据表。"},
    {"group": "老板/管理员", "task": "看【团队看板】和【审核素材库】：判断A/B/C，沉淀好标题、好封面、好选题、失败案例。", "submit_type": "admin", "standard": "员工少填，老板集中看；能入库的再入库。"},
]


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_text(v, limit=30000):
    if v is None:
        return ""
    if isinstance(v, (int, float, bool)):
        return v
    return str(v).strip()[:limit]



MATERIAL_TYPES = {
    "short_video": "短视频",
    "image": "图片",
    "copywriting": "文案",
    "other": "其他",
}

BUSINESS_TRACKS = {
    "beauty": "美业赛道",
    "direct_sales": "直销赛道",
    "local_life": "本地生活赛道",
    "ai": "AI赛道",
    "other": "其他赛道",
}


def _joined_text(item_or_data):
    if not isinstance(item_or_data, dict):
        return ""
    payload = item_or_data.get("payload") or item_or_data
    parts = []
    for k, v in payload.items():
        parts.append(str(k)); parts.append(str(v))
    for f in item_or_data.get("files") or []:
        parts.append(str(f.get("category", ""))); parts.append(str(f.get("filename", ""))); parts.append(str(f.get("original_name", "")))
    parts.append(str(item_or_data.get("type", ""))); parts.append(str(item_or_data.get("type_name", "")))
    return "\n".join(parts).lower()


def infer_material_type(item_or_data):
    txt = _joined_text(item_or_data)
    payload = (item_or_data.get("payload") or item_or_data) if isinstance(item_or_data, dict) else {}
    files = item_or_data.get("files") or [] if isinstance(item_or_data, dict) else []
    if any((f.get("category") or "").startswith("video") for f in files) or any(x in txt for x in ["视频", "口播", "脚本", "镜头", "分镜", "开头3秒", "抖音", "视频号", "小红书", "快手", "短视频"]):
        return "short_video"
    if any((f.get("category") or "").startswith("image") for f in files) or any(x in txt for x in ["图片", "封面", "海报", "提示词", "画面", "镜头", "构图", "png", "jpg", "jpeg", "webp"]):
        return "image"
    if any(x in txt for x in ["文案", "标题", "金句", "话术", "朋友圈", "招商", "成交", "私域", "客户原话", "content_text"]):
        return "copywriting"
    return "other"


def infer_business_track(item_or_data):
    txt = _joined_text(item_or_data)
    if any(x in txt for x in ["美容", "美业", "美容院", "皮肤", "护肤", "美甲", "美睫", "美体", "门店日常", "院长", "顾客到店"]):
        return "beauty"
    if any(x in txt for x in ["直销", "团队长", "招商", "会销", "起盘", "裂变", "代理", "复购", "囤兵", "高积粮", "成交团队"]):
        return "direct_sales"
    if any(x in txt for x in ["本地", "同城", "团购", "探店", "餐饮", "零食店", "实体店", "门店", "老板到店"]):
        return "local_life"
    if any(x in txt for x in ["ai", "人工智能", "智能体", "大模型", "提示词", "自动化", "数字员工", "运营中枢"]):
        return "ai"
    return "other"


def infer_quality_label(item_or_data, material_type=None, business_track=None):
    txt = _joined_text(item_or_data)
    material_type = material_type or infer_material_type(item_or_data)
    business_track = business_track or infer_business_track(item_or_data)
    score = 0
    reasons = []
    strong = ["金句", "成交", "招商", "复用", "开头", "钩子", "爆款", "信任", "复购", "客户", "痛点", "案例", "高积粮", "广囤兵", "结硬寨", "打呆仗"]
    weak = ["无", "不知道", "随便", "测试", "太泛", "风险", "不像", "不够", "需要修改", "真实感还是不太够"]
    for k in strong:
        if k.lower() in txt:
            score += 1; reasons.append(f"含高价值关键词：{k}")
            if score >= 4: break
    for k in weak:
        if k.lower() in txt:
            score -= 1; reasons.append(f"存在待优化/风险信号：{k}")
            if score <= -3: break
    if business_track in {"beauty", "direct_sales", "local_life", "ai"}:
        score += 1; reasons.append("赛道明确")
    else:
        score -= 1; reasons.append("赛道不明确或偏泛流量")
    content_len = len(txt)
    if content_len > 120:
        score += 1; reasons.append("内容信息量较足")
    if content_len < 40:
        score -= 1; reasons.append("内容过短")
    label = "A" if score >= 4 else ("C" if score <= -2 else "B")
    return label, "；".join(reasons[:5]) or "默认建议 B：需要人工复核"


def apply_classification(item, business_track=None, force_label=False):
    material_type = item.get("material_type") or infer_material_type(item)
    track = business_track or item.get("business_track") or infer_business_track(item)
    label, reason = infer_quality_label(item, material_type, track)
    item["material_type"] = material_type
    item["material_type_name"] = MATERIAL_TYPES.get(material_type, "其他")
    item["business_track"] = track
    item["business_track_name"] = BUSINESS_TRACKS.get(track, "其他赛道")
    item["ai_label"] = label
    item["ai_reason"] = reason
    if force_label or not item.get("label") or item.get("label") == "B":
        item["label"] = label
        item["status"] = "promote" if label == "A" else ("optimize" if label == "B" else "failure")
    return item


def write_jsonl_items(path, items):
    content = (
        "\n".join(json.dumps(x, ensure_ascii=False) for x in items)
        + ("\n" if items else "")
    ).encode("utf-8")
    atomic_write_bytes(path, content)


def upsert_library_item(path, item):
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    source_id = item.get("source_submission_id") or item.get("id")
    item = dict(item)
    item["source_submission_id"] = source_id
    item["library_time"] = now_iso()
    replaced = False
    for i, row in enumerate(rows):
        if (row.get("source_submission_id") or row.get("id")) == source_id:
            rows[i] = item; replaced = True; break
    if not replaced:
        rows.append(item)
    write_jsonl_items(path, rows)

def ensure_dirs(data_root):
    root = Path(data_root or "D:/HermesData") / "team_workbench"
    uploads = root / "uploads"
    libs = root / "libraries"
    for sub in [root, uploads, libs, uploads / "images", uploads / "videos", uploads / "documents", uploads / "screenshots"]:
        sub.mkdir(parents=True, exist_ok=True)
    return root, uploads, libs


def read_jsonl(path, limit=300):
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    items = []
    for line in reversed(lines[-limit:]):
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except Exception:
            pass
    return items


def append_jsonl(path, obj):
    content = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    atomic_append_bytes(path, content)


def normalize_submission(data):
    stype = safe_text(data.get("type"))
    if stype not in VALID_TYPES:
        raise ValueError("提交类型不正确")
    member = safe_text(data.get("member"), 100)
    if not member:
        raise ValueError("请填写/选择提交人")
    payload = data.get("payload") or {}
    if not isinstance(payload, dict):
        raise ValueError("payload 必须是对象")

    clean_payload = {}
    for k, v in payload.items():
        key = safe_text(k, 80)
        if key:
            clean_payload[key] = safe_text(v)

    label = safe_text(data.get("label") or clean_payload.get("asset_label") or "B", 5).upper()
    if label not in {"A", "B", "C"}:
        label = "B"
    review_status = safe_text(data.get("review_status") or clean_payload.get("review_status") or "pending_review", 40)
    status = "promote" if label == "A" else ("optimize" if label == "B" else "failure")
    item = {
        "id": uuid.uuid4().hex[:12],
        "time": now_iso(),
        "type": stype,
        "type_name": VALID_TYPES[stype],
        "member": member,
        "role": safe_text(data.get("role"), 80),
        "label": label,
        "status": status,
        "review_status": review_status,
        "payload": clean_payload,
        "files": data.get("files") or [],
        "notes": safe_text(data.get("notes")),
        "business_track": safe_text(data.get("business_track") or clean_payload.get("business_track"), 40),
    }
    apply_classification(item, business_track=item.get("business_track") or None, force_label=False)
    return item


def calc_stats(items):
    stats = {"total": len(items), "by_type": {}, "by_member": {}, "by_label": {"A": 0, "B": 0, "C": 0}, "by_material_type": {}, "by_business_track": {}, "pending_review": 0}
    for x in items:
        t = x.get("type_name") or x.get("type") or "未知"
        stats["by_type"][t] = stats["by_type"].get(t, 0) + 1
        m = x.get("member") or "未知"
        stats["by_member"][m] = stats["by_member"].get(m, 0) + 1
        lab = x.get("label") or "B"
        stats["by_label"][lab] = stats["by_label"].get(lab, 0) + 1
        mt = x.get("material_type_name") or MATERIAL_TYPES.get(x.get("material_type"), "未分类")
        stats["by_material_type"][mt] = stats["by_material_type"].get(mt, 0) + 1
        bt = x.get("business_track_name") or BUSINESS_TRACKS.get(x.get("business_track"), "未定赛道")
        stats["by_business_track"][bt] = stats["by_business_track"].get(bt, 0) + 1
        if x.get("review_status") == "pending_review":
            stats["pending_review"] += 1
    return stats


def flatten_for_csv(item):
    row = {k: item.get(k, "") for k in ["id", "time", "type", "type_name", "member", "role", "label", "status", "review_status", "notes"]}
    payload = item.get("payload") or {}
    for k, v in payload.items():
        row[f"payload_{k}"] = v
    files = item.get("files") or []
    row["files"] = json.dumps(files, ensure_ascii=False)
    return row


def classify_upload_dir(ext):
    ext = ext.lower()
    if ext in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return "images"
    if ext in {".mp4", ".mov", ".webm", ".m4v"}:
        return "videos"
    if ext in {".txt", ".md", ".pdf", ".doc", ".docx"}:
        return "documents"
    return "screenshots"


def to_int(v):
    try:
        if v is None or str(v).strip() == "":
            return 0
        return int(float(str(v).replace(",", "").strip()))
    except Exception:
        return 0


def normalize_video_item(data):
    item = {
        "id": uuid.uuid4().hex[:12],
        "time": now_iso(),
        "report_date": safe_text(data.get("report_date"), 20) or datetime.now().strftime("%Y-%m-%d"),
        "member": safe_text(data.get("member"), 100),
        "platform": safe_text(data.get("platform"), 30),
        "account_name": safe_text(data.get("account_name"), 120),
        "video_no": safe_text(data.get("video_no"), 20),
        "video_url": safe_text(data.get("video_url"), 1000),
        "title": safe_text(data.get("title"), 500),
        "topic": safe_text(data.get("topic"), 300),
        "cover_text": safe_text(data.get("cover_text"), 300),
        "opening_hook": safe_text(data.get("opening_hook"), 500),
        "publish_time": safe_text(data.get("publish_time"), 50),
        "views": to_int(data.get("views")),
        "likes": to_int(data.get("likes")),
        "comments": to_int(data.get("comments")),
        "favorites": to_int(data.get("favorites")),
        "shares": to_int(data.get("shares")),
        "dm_leads": to_int(data.get("dm_leads")),
        "followers_gain": to_int(data.get("followers_gain")),
        "deal_result": safe_text(data.get("deal_result"), 300),
        "grade": safe_text(data.get("grade") or "B", 5).upper(),
        "why": safe_text(data.get("why"), 1000),
        "tomorrow_action": safe_text(data.get("tomorrow_action"), 1000),
        "reusable_assets": safe_text(data.get("reusable_assets"), 1000),
    }
    if item["grade"] not in {"A", "B", "C"}:
        item["grade"] = "B"
    if not item["member"]:
        raise ValueError("请填写成员姓名")
    if not item["platform"]:
        raise ValueError("请选择平台")
    if not item["account_name"]:
        raise ValueError("请填写账号名称")
    if not (item["video_url"] or item["title"] or item["topic"]):
        raise ValueError("每条视频至少填写链接、标题或选题之一")
    item["interactions"] = item["likes"] + item["comments"] + item["favorites"] + item["shares"]
    return item


def calc_video_stats(items):
    stats = {
        "total_videos": len(items),
        "total_views": 0,
        "total_interactions": 0,
        "total_dm_leads": 0,
        "total_followers_gain": 0,
        "by_platform": {},
        "by_account": {},
        "by_grade": {"A": 0, "B": 0, "C": 0},
        "top_views": [],
        "top_interactions": [],
        "top_dm_leads": [],
    }
    def bucket(d, key):
        if key not in d:
            d[key] = {"videos": 0, "views": 0, "interactions": 0, "dm_leads": 0, "followers_gain": 0, "a_count": 0, "avg_views": 0}
        return d[key]
    for x in items:
        views = to_int(x.get("views")); inter = to_int(x.get("interactions")); dm = to_int(x.get("dm_leads")); fg = to_int(x.get("followers_gain"))
        stats["total_views"] += views; stats["total_interactions"] += inter; stats["total_dm_leads"] += dm; stats["total_followers_gain"] += fg
        grade = x.get("grade") or "B"; stats["by_grade"][grade] = stats["by_grade"].get(grade, 0) + 1
        for b in (bucket(stats["by_platform"], x.get("platform") or "未知"), bucket(stats["by_account"], (x.get("platform") or "未知") + "｜" + (x.get("account_name") or "未知"))):
            b["videos"] += 1; b["views"] += views; b["interactions"] += inter; b["dm_leads"] += dm; b["followers_gain"] += fg
            if grade == "A": b["a_count"] += 1
            b["avg_views"] = round(b["views"] / max(1, b["videos"]), 1)
    def slim(x):
        return {k: x.get(k, "") for k in ["id", "report_date", "member", "platform", "account_name", "title", "topic", "video_url", "views", "interactions", "dm_leads", "grade", "why"]}
    stats["top_views"] = [slim(x) for x in sorted(items, key=lambda z: to_int(z.get("views")), reverse=True)[:10]]
    stats["top_interactions"] = [slim(x) for x in sorted(items, key=lambda z: to_int(z.get("interactions")), reverse=True)[:10]]
    stats["top_dm_leads"] = [slim(x) for x in sorted(items, key=lambda z: to_int(z.get("dm_leads")), reverse=True)[:10]]
    return stats


def register_team_workbench_routes(app, project_dir, data_root=None):
    root, uploads, libs = ensure_dirs(data_root)
    submissions_path = root / "submissions.jsonl"
    video_items_path = root / "daily_video_items.jsonl"

    @app.route("/team-workbench")
    def team_workbench_page():
        return render_template("team_workbench.html")

    @app.route("/media/team-workbench/<category>/<path:filename>")
    def team_workbench_media(category, filename):
        base = uploads / category
        return send_from_directory(base, filename)

    @app.route("/api/team-workbench/upload", methods=["POST"])
    def team_workbench_upload():
        files = request.files.getlist("files") or []
        pending = []
        for f in files:
            original = f.filename or "upload"
            ext = Path(original).suffix.lower()
            if ext not in ALLOWED_UPLOAD_EXTS:
                return jsonify({"ok": False, "error": f"不支持的文件类型：{ext}"}), 400
            category = classify_upload_dir(ext)
            safe = secure_filename(original) or f"upload{ext}"
            filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}_{safe}"
            dest = uploads / category / filename
            pending.append((original, filename, category, dest, f.read()))
        saved = []
        with reserve_capacity(sum(len(item[4]) for item in pending)) as reservation:
            for original, filename, category, dest, content in pending:
                atomic_write_bytes(dest, content, reservation=reservation)
                mime = mimetypes.guess_type(str(dest))[0] or "application/octet-stream"
                saved.append({
                    "original_name": original,
                    "filename": filename,
                    "category": category,
                    "mime": mime,
                    "bytes": len(content),
                    "path": str(dest),
                    "url": f"/media/team-workbench/{category}/{filename}",
                })
        return jsonify({"ok": True, "files": saved})

    @app.route("/api/team-workbench/status")
    def team_workbench_status():
        items = read_jsonl(submissions_path, limit=5000)
        return jsonify({
            "ok": True,
            "version": "V2",
            "data_root": str(root),
            "submissions_path": str(submissions_path),
            "uploads_path": str(uploads),
            "libraries_path": str(libs),
            "types": VALID_TYPES,
            "material_types": MATERIAL_TYPES,
            "business_tracks": BUSINESS_TRACKS,
            "tasks": TODAY_TASKS,
            "stats": calc_stats(items),
        })

    @app.route("/api/team-workbench/submit", methods=["POST"])
    def team_workbench_submit():
        try:
            item = normalize_submission(request.get_json(force=True, silent=True) or {})
            append_jsonl(submissions_path, item)
            if item["label"] == "A":
                upsert_library_item(libs / f"{item['type']}.jsonl", item)
            return jsonify({"ok": True, "item": item, "message": "提交成功"})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400

    @app.route("/api/team-workbench/submissions")
    def team_workbench_submissions():
        items = read_jsonl(submissions_path, limit=int(request.args.get("limit", 300)))
        qtype = request.args.get("type") or ""
        member = request.args.get("member") or ""
        label = request.args.get("label") or ""
        review = request.args.get("review_status") or ""
        material_type = request.args.get("material_type") or ""
        business_track = request.args.get("business_track") or ""
        if qtype:
            items = [x for x in items if x.get("type") == qtype]
        if member:
            items = [x for x in items if member in (x.get("member") or "")]
        if label:
            items = [x for x in items if x.get("label") == label]
        if review:
            items = [x for x in items if x.get("review_status") == review]
        if material_type:
            items = [x for x in items if x.get("material_type") == material_type]
        if business_track:
            items = [x for x in items if x.get("business_track") == business_track]
        return jsonify({"ok": True, "items": items, "stats": calc_stats(items)})

    @app.route("/api/team-workbench/review", methods=["POST"])
    def team_workbench_review():
        try:
            data = request.get_json(force=True, silent=True) or {}
            sub_id = safe_text(data.get("id"), 100)
            if not sub_id:
                return jsonify({"ok": False, "error": "缺少 submission id"}), 400
            if not submissions_path.exists():
                return jsonify({"ok": False, "error": "暂无提交记录"}), 404
            rows = []
            found = False
            updated = None
            for line in submissions_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("id") == sub_id:
                    label = safe_text(data.get("label") or obj.get("label") or "B", 5).upper()
                    if label not in {"A", "B", "C"}:
                        label = "B"
                    material_type = safe_text(data.get("material_type") or obj.get("material_type") or infer_material_type(obj), 40)
                    if material_type not in MATERIAL_TYPES:
                        material_type = "other"
                    business_track = safe_text(data.get("business_track") or obj.get("business_track") or infer_business_track(obj), 40)
                    if business_track not in BUSINESS_TRACKS:
                        business_track = "other"
                    obj["material_type"] = material_type
                    obj["material_type_name"] = MATERIAL_TYPES.get(material_type, "其他")
                    obj["business_track"] = business_track
                    obj["business_track_name"] = BUSINESS_TRACKS.get(business_track, "其他赛道")
                    ai_label, ai_reason = infer_quality_label(obj, material_type, business_track)
                    obj["ai_label"] = ai_label
                    obj["ai_reason"] = ai_reason
                    obj["label"] = label
                    obj["status"] = "promote" if label == "A" else ("optimize" if label == "B" else "failure")
                    obj["review_status"] = safe_text(data.get("review_status") or "approved", 40)
                    obj["review_notes"] = safe_text(data.get("review_notes"), 5000)
                    obj["reviewer"] = safe_text(data.get("reviewer"), 100)
                    obj["review_time"] = now_iso()
                    found = True
                    updated = obj
                rows.append(obj)
            if not found:
                return jsonify({"ok": False, "error": f"未找到 id={sub_id} 的提交"}), 404
            write_jsonl_items(submissions_path, rows)
            if updated and updated.get("label") == "A":
                upsert_library_item(libs / f"{updated['type']}.jsonl", updated)
            return jsonify({"ok": True, "item": updated, "message": "审核已保存"})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400

    @app.route("/api/team-workbench/auto-classify", methods=["POST"])
    def team_workbench_auto_classify():
        try:
            data = request.get_json(force=True, silent=True) or {}
            only_missing = bool(data.get("only_missing", True))
            if not submissions_path.exists():
                return jsonify({"ok": True, "count": 0, "items": []})
            rows = []
            changed = 0
            for line in submissions_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if (not only_missing) or (not obj.get("material_type") or not obj.get("ai_label")):
                    before = json.dumps(obj, ensure_ascii=False, sort_keys=True)
                    apply_classification(obj, business_track=obj.get("business_track") or None, force_label=False)
                    after = json.dumps(obj, ensure_ascii=False, sort_keys=True)
                    if before != after:
                        changed += 1
                rows.append(obj)
            write_jsonl_items(submissions_path, rows)
            return jsonify({"ok": True, "count": changed, "stats": calc_stats(rows), "message": f"已自动初分 {changed} 条"})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400

    @app.route("/api/team-workbench/video-items", methods=["POST"])
    def team_workbench_video_items_submit():
        try:
            data = request.get_json(force=True, silent=True) or {}
            rows = data.get("items") or []
            if not isinstance(rows, list) or not rows:
                return jsonify({"ok": False, "error": "请至少填写一条视频数据"}), 400
            saved = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                item = normalize_video_item(row)
                append_jsonl(video_items_path, item)
                saved.append(item)
            return jsonify({"ok": True, "items": saved, "count": len(saved), "message": "每日视频数据已保存"})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400

    @app.route("/api/team-workbench/video-items")
    def team_workbench_video_items_list():
        items = read_jsonl(video_items_path, limit=int(request.args.get("limit", 1000)))
        date = request.args.get("date") or ""
        platform = request.args.get("platform") or ""
        account = request.args.get("account") or ""
        member = request.args.get("member") or ""
        if date:
            items = [x for x in items if x.get("report_date") == date]
        if platform:
            items = [x for x in items if x.get("platform") == platform]
        if account:
            items = [x for x in items if account in (x.get("account_name") or "")]
        if member:
            items = [x for x in items if member in (x.get("member") or "")]
        return jsonify({"ok": True, "items": items, "stats": calc_video_stats(items), "path": str(video_items_path)})

    @app.route("/api/team-workbench/video-stats")
    def team_workbench_video_stats():
        items = read_jsonl(video_items_path, limit=int(request.args.get("limit", 5000)))
        date = request.args.get("date") or ""
        if date:
            items = [x for x in items if x.get("report_date") == date]
        return jsonify({"ok": True, "stats": calc_video_stats(items), "items_count": len(items), "path": str(video_items_path)})

    @app.route("/api/team-workbench/video-items.csv")
    def team_workbench_video_items_csv():
        items = list(reversed(read_jsonl(video_items_path, limit=20000)))
        keys = ["id", "time", "report_date", "member", "platform", "account_name", "video_no", "video_url", "title", "topic", "cover_text", "opening_hook", "publish_time", "views", "likes", "comments", "favorites", "shares", "interactions", "dm_leads", "followers_gain", "deal_result", "grade", "why", "tomorrow_action", "reusable_assets"]
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        for row in items:
            writer.writerow(row)
        return Response(output.getvalue(), mimetype="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=daily_video_items.csv"})

    @app.route("/api/team-workbench/daily-video.csv")
    def team_workbench_daily_video_csv():
        items = [x for x in reversed(read_jsonl(submissions_path, limit=10000)) if x.get("type") == "daily_video_report"]
        rows = [flatten_for_csv(x) for x in items]
        keys = []
        for row in rows:
            for k in row.keys():
                if k not in keys:
                    keys.append(k)
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=keys or ["empty"], extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        return Response(output.getvalue(), mimetype="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=daily_video_reports.csv"})

    @app.route("/api/team-workbench/export.csv")
    def team_workbench_export_csv():
        items = list(reversed(read_jsonl(submissions_path, limit=10000)))
        rows = [flatten_for_csv(x) for x in items]
        keys = []
        for row in rows:
            for k in row.keys():
                if k not in keys:
                    keys.append(k)
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=keys or ["empty"], extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        return Response(output.getvalue(), mimetype="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=team_workbench_submissions.csv"})
