"""Batch asset operations for the content API."""

import io
import json
import pathlib
import re
import time
import urllib.parse
import zipfile
from contextlib import closing


def _clean_asset_id(asset_id):
    try:
        return int(asset_id)
    except Exception:
        raise ValueError("缺少资产标识")


def _asset_rel_from_url(url):
    url = str(url or "").strip()
    if not url:
        return ""
    if url.startswith("/api/gen/file/"):
        return urllib.parse.unquote(url[len("/api/gen/file/"):]).replace("\\", "/").lstrip("/")
    try:
        u = urllib.parse.urlparse(url)
        if u.path.startswith("/api/gen/file/"):
            return urllib.parse.unquote(u.path[len("/api/gen/file/"):]).replace("\\", "/").lstrip("/")
    except Exception:
        pass
    return ""


def _asset_file_url(core, rel, stored_url="", private=False):
    stored_url = str(stored_url or "").strip()
    rel = str(rel or "").replace("\\", "/").lstrip("/")
    if private and rel:
        try:
            from . import cos
            if cos.enabled():
                return cos.object_url(rel, private=True)
        except Exception:
            pass
    if stored_url and not stored_url.startswith("/api/gen/file/"):
        return stored_url
    return core._file_url(rel) if rel else stored_url


def _zip_name(text, default_name):
    text = re.sub(r"\s+", "_", str(text or "").strip())
    text = re.sub(r'[\\/:*?"<>|]+', "_", text).strip("._")
    return (text or default_name)[:80]


BATCH_KINDS = {"image", "audio", "video", "avatar"}   # 有文件、能打包/删除的资产类型


def _asset_download_record(core, username, kind, asset_id):
    kind = core._clean_asset_kind(kind)
    asset_id = _clean_asset_id(asset_id)
    # _clean_asset_kind 放行的类型比这里能处理的多(copy/collect/leads 随统一 assets 表接入了收藏打标)。
    # 不显式挡住的话，它们会一路落到函数末尾的 avatars 分支：id 空间不同，多数报「资产不存在」，
    # 但只要用户恰好有同 id 的数字人形象，就会把那个头像当成采集素材打包/删除 —— 静默的数据错配。
    if kind not in BATCH_KINDS:
        raise LookupError("「%s」类资产没有文件，不支持批量下载/删除" % kind)
    if kind == "image":
        with closing(core.jdb()) as c:
            core._ensure_column(c, "jobs", "deleted", "INTEGER DEFAULT 0")
            row = c.execute("""SELECT id,result,created_at FROM jobs
                               WHERE id=? AND username=? AND kind='image' AND status='done'
                                 AND COALESCE(deleted,0)=0""",
                            (asset_id, username)).fetchone()
        if not row:
            raise LookupError("资产不存在或不属于当前账号")
        try:
            res = json.loads(row["result"] or "{}")
        except Exception:
            res = {}
        files = []
        for rel in (res.get("files") or []):
            if rel:
                files.append(str(rel))
        if res.get("file"):
            files.append(str(res.get("file")))
        urls = [u for u in (res.get("urls") or []) if u]
        if res.get("url"):
            urls.append(res.get("url"))
        for u in urls:
            rel = _asset_rel_from_url(u)
            if rel:
                files.append(rel)
        return {
            "kind": kind, "id": asset_id,
            "title": res.get("prompt") or ("image-%s" % asset_id),
            "files": [(rel, "image-%s" % asset_id) for rel in files],
            "urls": urls,
        }
    if kind == "audio":
        with closing(core.adb()) as c:
            core._ensure_column(c, "audio_assets", "deleted", "INTEGER DEFAULT 0")
            row = c.execute("""SELECT id,file,url,text FROM audio_assets
                               WHERE id=? AND username=? AND COALESCE(deleted,0)=0""",
                            (asset_id, username)).fetchone()
        if not row:
            raise LookupError("资产不存在或不属于当前账号")
        rel = row["file"] or _asset_rel_from_url(row["url"])
        url = _asset_file_url(core, rel, row["url"]) if rel else row["url"]
        return {
            "kind": kind, "id": asset_id,
            "title": row["text"] or ("audio-%s" % asset_id),
            "files": [(rel, "audio-%s" % asset_id)] if rel else [],
            "urls": [url] if url else [],
        }
    if kind == "video":
        with closing(core.adb()) as c:
            row = c.execute("""SELECT id,video_file,video_url,image_file,text,mode FROM video_assets
                               WHERE id=? AND username=? AND status!='deleted'""",
                            (asset_id, username)).fetchone()
        if not row:
            raise LookupError("资产不存在或不属于当前账号")
        rels = []
        if row["video_file"]:
            rels.append((row["video_file"], "video-%s" % asset_id))
        elif row["image_file"]:
            rels.append((row["image_file"], "video-cover-%s" % asset_id))
        urls = []
        if row["video_file"] or row["video_url"]:
            urls.append(_asset_file_url(core, row["video_file"], row["video_url"], private=True))
        return {
            "kind": kind, "id": asset_id,
            "title": row["text"] or row["mode"] or ("video-%s" % asset_id),
            "files": rels,
            "urls": [u for u in urls if u],
        }
    with closing(core.adb()) as c:
        row = c.execute("""SELECT id,name,image_file FROM avatars
                           WHERE id=? AND username=? AND status!='deleted'""",
                        (asset_id, username)).fetchone()
    if not row:
        raise LookupError("资产不存在或不属于当前账号")
    rel = row["image_file"]
    return {
        "kind": kind, "id": asset_id,
        "title": row["name"] or ("avatar-%s" % asset_id),
        "files": [(rel, "avatar-%s" % asset_id)] if rel else [],
        "urls": [core._file_url(rel)] if rel else [],
    }


def _requested_assets(core, body):
    assets = body.get("assets")
    if not isinstance(assets, list):
        kind = body.get("kind")
        assets = [{"kind": kind, "id": item} for item in (body.get("ids") or [])]
    out = []
    seen = set()
    for item in assets or []:
        if not isinstance(item, dict):
            continue
        try:
            kind = core._clean_asset_kind(item.get("kind"))
            asset_id = _clean_asset_id(item.get("id"))
        except Exception:
            continue
        key = (kind, asset_id)
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": kind, "id": asset_id})
        if len(out) >= 120:
            break
    if not out:
        raise ValueError("请选择资产")
    return out


def build_asset_zip(core, username, body):
    requested = _requested_assets(core, body)
    buf = io.BytesIO()
    manifest = []
    added = set()
    added_paths = set()
    errors = []
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in requested:
            try:
                rec = _asset_download_record(core, username, item["kind"], item["id"])
            except Exception as e:
                errors.append({"kind": item["kind"], "id": item["id"], "detail": str(e)[:120]})
                continue
            base = _zip_name(rec.get("title"), "%s-%s" % (rec["kind"], rec["id"]))
            wrote_file = False
            for rel, label in rec.get("files") or []:
                fp = core._resolve_out_file(rel)
                if not fp:
                    continue
                try:
                    canonical = fp.resolve().relative_to(core.OUT_DIR.resolve()).as_posix()
                except Exception:
                    continue
                if canonical in added_paths:
                    continue
                ext = fp.suffix or pathlib.Path(str(rel)).suffix or ".bin"
                arc = "%s/%s%s" % (rec["kind"], _zip_name(base, label), ext)
                n = 2
                while arc in added:
                    arc = "%s/%s-%d%s" % (rec["kind"], _zip_name(base, label), n, ext)
                    n += 1
                zf.write(fp, arc)
                added.add(arc)
                added_paths.add(canonical)
                wrote_file = True
                manifest.append("%s#%s: %s" % (rec["kind"], rec["id"], core._file_url(canonical)))
            urls = [u for u in (rec.get("urls") or []) if u]
            if urls and not wrote_file:
                manifest.extend("%s#%s: %s" % (rec["kind"], rec["id"], u) for u in urls)
        if manifest:
            zf.writestr("下载链接清单.txt", "\n".join(manifest) + "\n")
        if errors:
            zf.writestr("跳过清单.txt", "\n".join("%s#%s %s" % (e["kind"], e["id"], e["detail"]) for e in errors) + "\n")
        if not added and not manifest:
            raise LookupError("所选资产没有可下载文件")
    return buf.getvalue(), {"count": len(requested), "files": len(added), "skipped": len(errors)}


def batch_delete_user_assets(core, username, body):
    requested = _requested_assets(core, body)
    deleted, failed = [], []
    for item in requested:
        try:
            if item["kind"] == "avatar":
                asset = core._domains()[2].delete_video_avatar(username, item["id"])
                core._delete_asset_mark(username, "avatar", str(item["id"]))
                deleted.append({"kind": "avatar", "id": asset.get("id"), "deleted": True})
            else:
                deleted.append(core.delete_user_asset(username, item["kind"], item["id"]))
        except Exception as e:
            failed.append({"kind": item["kind"], "id": item["id"], "detail": str(e)[:120]})
    return {"deleted": deleted, "failed": failed}
