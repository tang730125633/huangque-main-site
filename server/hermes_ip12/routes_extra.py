# routes_extra.py - v6 endpoints

def register_v6_routes(app):
    from flask import jsonify, render_template, request

    def api_status():
        from model_router import current_provider, provider_status
        providers = provider_status()
        return jsonify(ok=bool(providers), provider=current_provider(), providers=providers, version="v6")
    app.add_url_rule("/api/status", "api_status", api_status)

    def video_factory_page():
        return render_template("video_factory.html")
    app.add_url_rule("/video-factory", "video_factory_page", video_factory_page)

    def pipeline_page():
        return render_template("pipeline.html")
    app.add_url_rule("/pipeline", "pipeline_page", pipeline_page)

    def api_pipeline_upload():
        try:
            if 'video' not in request.files:
                return jsonify({"ok": False, "error": "No video file"}), 400
            f = request.files['video']
            topic = request.form.get('topic', '').strip()
            niche = request.form.get('niche', '美业').strip()

            from pathlib import Path
            from werkzeug.utils import secure_filename
            from artifact_store import atomic_write_bytes, new_asset_id, upload_path
            from security import current_username
            uid = new_asset_id()
            filename = secure_filename(f.filename or "video.mp4")
            if Path(filename).suffix.lower() not in {".mp4", ".mov", ".webm", ".mkv", ".avi"}:
                return jsonify({"ok": False, "error": "Unsupported video type"}), 400
            save_path = upload_path(current_username(), uid, Path(filename).suffix)
            atomic_write_bytes(save_path, f.read())
            return jsonify({"ok": True, "upload_id": uid})
        except OSError as e:
            if "quota" in str(e).lower():
                return jsonify({"ok": False, "error": str(e)}), 507
            raise
        except Exception as e:
            import traceback
            traceback.print_exc()
            return jsonify({"ok": False, "error": str(e)}), 500

    app.add_url_rule("/api/pipeline-upload", "api_pipeline_upload", api_pipeline_upload, methods=["POST"])

    try: from skill_modules import register_skills; register_skills(app); print("skills OK")
    except Exception as e: print("skills:", e)
    try: from video_factory import register_video_factory; register_video_factory(app); print("vf OK")
    except Exception as e: print("vf:", e)
    try: from video_analyzer import register_video_analyzer; register_video_analyzer(app); print("va OK")
    except Exception as e: print("va:", e)
    try: from image_services import register_image_routes; register_image_routes(app); print("img OK")
    except Exception as e: print("img:", e)
    try: from video_services import register_video_routes; register_video_routes(app); print("vs OK")
    except Exception as e: print("vs:", e)
    try: from video_pipeline import register_pipeline; register_pipeline(app); print("vp OK")
    except Exception as e: print("vp:", e)
    try: from video_replica import register_replica; register_replica(app); print("replica OK")
    except Exception as e: print("replica:", e)

    try: from media_library import register_media; register_media(app); print("media OK")
    except Exception as e: print("media:", e)
    try: from api_upload import register_upload; register_upload(app); print("upload OK")
    except Exception as e: print("upload:", e)
    print("All v6 routes registered")
