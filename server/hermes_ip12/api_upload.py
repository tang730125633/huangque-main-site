#!/usr/bin/env python3
def register_upload(app):
    from flask import request, jsonify
    import base64 as b64, binascii
    from pathlib import Path
    from werkzeug.utils import secure_filename
    from media_library import MediaLibrary
    from artifact_store import StorageQuotaExceeded, atomic_write_bytes, media_path, new_asset_id
    from security import current_username

    @app.route("/api/media/upload", methods=["POST"])
    def api_media_upload():
        data = request.get_json() or {}
        keyword = str(data.get("keyword", "unknown"))
        filename = secure_filename(str(data.get("filename", "image.jpg")))
        img_b64 = data.get("data", "")
        if not img_b64:
            return jsonify(ok=False, error="No data"), 400
        if len(img_b64) > 20_000_000:
            return jsonify(ok=False, error="Image too large"), 413
        if Path(filename).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            return jsonify(ok=False, error="Unsupported image type"), 400
        try:
            image = b64.b64decode(img_b64, validate=True)
        except (binascii.Error, ValueError):
            return jsonify(ok=False, error="Invalid base64 data"), 400
        username = current_username()
        dest = media_path(username, new_asset_id(), Path(filename).suffix)
        try:
            atomic_write_bytes(dest, image)
            mid = MediaLibrary.add(
                keyword, str(dest), source="upload",
                owner_username=username, copy_file=False,
            )
            return jsonify(ok=True, id=mid)
        except StorageQuotaExceeded as exc:
            dest.unlink(missing_ok=True)
            return jsonify(ok=False, error=str(exc)), 507
        except Exception:
            dest.unlink(missing_ok=True)
            raise

    print("api_upload route OK")
