#!/usr/bin/env python3
"""
media_library.py — 素材库 + 知识库
/data/media_library/   按 owner 隔离的素材文件(图/视频)
/data/knowledge/       视觉公式/模板/关键词映射
"""
import os, json, time
from pathlib import Path
from datetime import datetime
from runtime_paths import DATA_DIR
from artifact_store import (
    atomic_copy,
    atomic_write_bytes,
    media_path,
    new_asset_id,
    owner_key,
    storage_transaction,
)
from werkzeug.utils import secure_filename

BASE = DATA_DIR
MEDIA = BASE / "media_library"
KNOWLEDGE = BASE / "knowledge"

for d in [MEDIA, KNOWLEDGE]:
    d.mkdir(parents=True, exist_ok=True)

# ── 素材库 ──
class MediaLibrary:
    """管理素材文件的存储、搜索、去重"""

    INDEX_FILE = MEDIA / "index.json"

    @staticmethod
    def _load():
        if MediaLibrary.INDEX_FILE.exists():
            return json.loads(MediaLibrary.INDEX_FILE.read_text())
        return {"entries": {}, "keywords": {}}

    @staticmethod
    def _save(data):
        atomic_write_bytes(
            MediaLibrary.INDEX_FILE,
            json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    @staticmethod
    def _owner(owner_username=None):
        if owner_username is not None:
            return str(owner_username)
        try:
            from security import current_username
            return current_username()
        except Exception:
            return ""

    @staticmethod
    def search(keyword, owner_username=None):
        """搜素材库，返回匹配的文件列表"""
        data = MediaLibrary._load()
        kw = keyword.lower().strip()
        results = []
        owner_username = MediaLibrary._owner(owner_username)
        for entry_id, entry in data["entries"].items():
            if entry.get("owner_username") != owner_username:
                continue
            tags = [t.lower() for t in entry.get("tags", [])]
            if kw in entry.get("keyword", "").lower() or any(kw in t for t in tags):
                results.append(entry)
        return results

    @staticmethod
    def add(keyword, file_path, source="manual", tags=None, owner_username=None, copy_file=True):
        """添加素材入库。file_path会被复制到素材库"""
        owner_username = MediaLibrary._owner(owner_username)
        if not owner_username:
            raise ValueError("media owner required")
        with storage_transaction():
            data = MediaLibrary._load()

            # 去重检查与索引提交必须处于同一个事务，避免并发丢失更新。
            fhash = str(os.path.getsize(file_path)) + "_" + Path(file_path).name
            for entry in data["entries"].values():
                if (
                    entry.get("fhash") == fhash
                    and entry.get("owner_username") == owner_username
                ):
                    return entry["id"]

            safe_kw = secure_filename(str(keyword).lower())[:30] or "unknown"
            owner_id = owner_key(owner_username)
            owner_dir = (MEDIA / owner_id).resolve()
            kw_dir = (owner_dir / safe_kw).resolve()
            if kw_dir.parent != owner_dir:
                raise ValueError("invalid media keyword")
            kw_dir.mkdir(parents=True, exist_ok=True)

            ext = Path(file_path).suffix
            entry_id = f"{owner_id}_{new_asset_id()}"
            copied = False
            if copy_file:
                dest = (kw_dir / f"{entry_id}{ext}").resolve()
                if dest.parent != kw_dir:
                    raise ValueError("invalid media destination")
                atomic_copy(file_path, dest)
                copied = True
            else:
                dest = Path(file_path).resolve()
                if not dest.is_relative_to(DATA_DIR.resolve()):
                    raise ValueError("media file must be inside the data directory")

            stat = os.stat(dest)
            entry = {
                "id": entry_id,
                "owner_username": owner_username,
                "keyword": keyword,
                "file_path": str(dest),
                "original_name": Path(file_path).name,
                "source": source,
                "tags": tags or [keyword],
                "size_bytes": stat.st_size,
                "format": ext.lstrip('.'),
                "added_at": datetime.now().isoformat(),
                "fhash": fhash,
                "use_count": 0
            }
            data["entries"][entry_id] = entry
            data.setdefault("keywords", {}).setdefault(keyword, []).append(entry_id)
            try:
                MediaLibrary._save(data)
            except Exception:
                if copied or not copy_file:
                    dest.unlink(missing_ok=True)
                raise
            return entry_id

    @staticmethod
    def increment_use(entry_id, owner_username=None):
        owner_username = MediaLibrary._owner(owner_username)
        with storage_transaction():
            data = MediaLibrary._load()
            entry = data["entries"].get(entry_id)
            if not entry or entry.get("owner_username") != owner_username:
                return False
            entry["use_count"] = entry.get("use_count", 0) + 1
            MediaLibrary._save(data)
            return True

    @staticmethod
    def stats(owner_username=None):
        """返回素材库统计"""
        data = MediaLibrary._load()
        owner_username = MediaLibrary._owner(owner_username)
        entries = {
            key: value for key, value in data["entries"].items()
            if value.get("owner_username") == owner_username
        }
        total_size = sum(e.get("size_bytes", 0) for e in entries.values())
        keywords = sorted({
            str(entry.get("keyword", ""))
            for entry in entries.values()
            if str(entry.get("keyword", "")).strip()
        })
        return {
            "total_files": len(entries),
            "total_keywords": len(keywords),
            "total_size_mb": round(total_size / 1024 / 1024, 1),
            "keywords": keywords[:20]
        }

# ── 知识库 ──
class KnowledgeBase:
    """管理视觉公式、脚本模板、关键词映射"""

    @staticmethod
    def add_formula(video_title, visual_formula, source_url=""):
        """存储对标视频的视觉公式"""
        formulas = KnowledgeBase._load_json("visual_formulas.json")

        fid = f"formula_{int(time.time())}"
        formulas[fid] = {
            "id": fid,
            "title": video_title,
            "source_url": source_url,
            "formula": visual_formula,
            "created_at": datetime.now().isoformat()
        }
        KnowledgeBase._save_json("visual_formulas.json", formulas)
        return fid

    @staticmethod
    def add_script_template(name, template_json, niche="通用"):
        """存储脚本模板"""
        templates = KnowledgeBase._load_json("script_templates.json")
        tid = f"template_{int(time.time())}"
        templates[tid] = {
            "id": tid,
            "name": name,
            "niche": niche,
            "template": template_json,
            "created_at": datetime.now().isoformat()
        }
        KnowledgeBase._save_json("script_templates.json", templates)
        return tid

    @staticmethod
    def add_keyword_map(chinese_word, english_search_term, best_source="pexels"):
        """存储中→英关键词映射"""
        maps = KnowledgeBase._load_json("keyword_map.json")
        maps[chinese_word] = {
            "english": english_search_term,
            "best_source": best_source,
            "updated_at": datetime.now().isoformat(),
            "use_count": maps.get(chinese_word, {}).get("use_count", 0)
        }
        KnowledgeBase._save_json("keyword_map.json", maps)

    @staticmethod
    def get_keyword_map(chinese_word):
        """查询关键词映射"""
        maps = KnowledgeBase._load_json("keyword_map.json")
        return maps.get(chinese_word)

    @staticmethod
    def get_formulas(niche=None):
        """获取所有视觉公式"""
        formulas = KnowledgeBase._load_json("visual_formulas.json")
        if niche:
            return {k: v for k, v in formulas.items() if niche in v.get("title", "")}
        return formulas

    @staticmethod
    def stats():
        return {
            "formulas": len(KnowledgeBase._load_json("visual_formulas.json")),
            "templates": len(KnowledgeBase._load_json("script_templates.json")),
            "keyword_maps": len(KnowledgeBase._load_json("keyword_map.json"))
        }

    @staticmethod
    def _load_json(filename):
        path = KNOWLEDGE / filename
        if path.exists():
            return json.loads(path.read_text())
        return {}

    @staticmethod
    def _save_json(filename, data):
        (KNOWLEDGE / filename).write_text(json.dumps(data, ensure_ascii=False, indent=2))

# ── Google Custom Search ──
GOOGLE_API_KEY = os.environ.get("HERMES_GOOGLE_API_KEY", "")
GOOGLE_CX = os.environ.get("HERMES_GOOGLE_CX", "e2f1e71d4c78a4617")
PEXELS_KEY = os.environ.get("PEXELS_API_KEY", "")

def google_search_images(query, num=5):
    """搜Google图片，返回[{url, title, width, height, thumbnail}]"""
    import requests as req
    try:
        r = req.get("https://www.googleapis.com/customsearch/v1",
            params={"key": GOOGLE_API_KEY, "cx": GOOGLE_CX, "q": query,
                    "searchType": "image", "num": num, "imgSize": "medium"},
            timeout=10)
        if r.status_code == 200:
            items = r.json().get("items", [])
            return [{
                "url": it.get("link", ""),
                "title": it.get("title", ""),
                "width": it.get("image", {}).get("width", 0),
                "height": it.get("image", {}).get("height", 0),
                "thumbnail": it.get("image", {}).get("thumbnailLink", "")
            } for it in items]
        else:
            print(f"Google search HTTP {r.status_code}: {r.text[:100]}")
    except Exception as e:
        print(f"Google search error: {e}")
    return []

# ── 一键出图调度器 ──
def get_best_image(keyword):
    """
    为关键词获取最佳配图：
    1. 查素材库 → 有则直接返回
    2. 查关键词映射 → 用翻译后的英文搜
    3. Pexels → Google → 下载入库
    """
    import requests as req
    owner_username = MediaLibrary._owner()
    if not owner_username:
        raise ValueError("media owner required")

    # 1. 查素材库
    cached = MediaLibrary.search(keyword, owner_username=owner_username)
    if cached:
        entry = cached[0]
        MediaLibrary.increment_use(entry["id"], owner_username=owner_username)
        return {"source": "library", "path": entry["file_path"], "keyword": keyword}

    # 2. 查关键词映射
    mapping = KnowledgeBase.get_keyword_map(keyword)
    search_term = mapping["english"] if mapping else keyword

    # 3. 搜 Pexels（免费素材优先）
    try:
        if not PEXELS_KEY:
            raise RuntimeError("Pexels API key is not configured")
        r = req.get("https://api.pexels.com/v1/search",
            headers={"Authorization": PEXELS_KEY},
            params={"query": search_term, "per_page": 3, "orientation": "portrait"},
            timeout=10)
        if r.status_code == 200:
            photos = r.json().get("photos", [])
            if photos:
                photo = photos[0]
                img_url = photo["src"]["large"]
                img_data = req.get(img_url, timeout=30).content
                saved_path = media_path(owner_username, new_asset_id(), ".jpg")
                atomic_write_bytes(saved_path, img_data)
                MediaLibrary.add(
                    keyword, str(saved_path), source="pexels",
                    tags=[search_term, photo.get("photographer", "")],
                    owner_username=owner_username, copy_file=False,
                )
                return {"source": "pexels", "keyword": keyword, "count": len(photos)}
    except Exception as e:
        print(f"Pexels error: {e}")

    # 4. 搜 Google（全网真实素材）
    google_imgs = google_search_images(search_term, num=5)
    if google_imgs:
        for img in google_imgs:
            try:
                img_data = req.get(img["url"], timeout=30, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                }).content
                if len(img_data) > 5000:  # at least 5KB, skip placeholders
                    saved_path = media_path(owner_username, new_asset_id(), ".jpg")
                    atomic_write_bytes(saved_path, img_data)
                    MediaLibrary.add(
                        keyword, str(saved_path), source="google",
                        tags=[search_term, img.get("title", "")],
                        owner_username=owner_username, copy_file=False,
                    )
                    return {"source": "google", "keyword": keyword, "url": img["url"]}
            except Exception as ie:
                continue

    # 5. 无结果
    return {"source": "none", "keyword": keyword}

# ── API routes ──
def register_media(app):
    from flask import request, jsonify

    @app.route("/api/media/search")
    def api_media_search():
        kw = request.args.get("q", "")
        results = MediaLibrary.search(kw)
        return jsonify({"ok": True, "keyword": kw, "results": results})

    @app.route("/api/media/stats")
    def api_media_stats():
        return jsonify({
            "ok": True,
            "media": MediaLibrary.stats(),
            "knowledge": KnowledgeBase.stats()
        })

    @app.route("/api/knowledge/formula", methods=["POST"])
    def api_add_formula():
        data = request.get_json() or {}
        fid = KnowledgeBase.add_formula(
            data.get("title", ""),
            data.get("formula", {}),
            data.get("url", "")
        )
        return jsonify({"ok": True, "id": fid})

    print("media_library routes OK")
