#!/usr/bin/env python3
def register_upload(app):
    from flask import request, jsonify
    import base64 as b64, binascii
    from pathlib import Path
    from werkzeug.utils import secure_filename
    from media_library import MediaLibrary
    from runtime_paths import ROOT_DIR

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
        safe_kw = secure_filename(keyword)[:30] or "unknown"
        kw_dir = (ROOT_DIR / "media_library" / safe_kw).resolve()
        kw_dir.mkdir(parents=True, exist_ok=True)
        dest = (kw_dir / filename).resolve()
        if dest.parent != kw_dir:
            return jsonify(ok=False, error="Invalid filename"), 400
        dest.write_bytes(image)
        try:
            mid = MediaLibrary.add(keyword, str(dest), source="bing")
            return jsonify(ok=True, id=mid, path=str(dest))
        except:
            return jsonify(ok=True, path=str(dest))

    print("api_upload route OK")
