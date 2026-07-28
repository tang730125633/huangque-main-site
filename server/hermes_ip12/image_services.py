"""独立图片服务模块 - 挂载到Flask app"""
import urllib.parse
import requests as http_requests
from flask import request, jsonify, render_template

def register_image_routes(app):

    @app.route("/images")
    def images_page():
        return render_template("images.html")

    @app.route("/api/generate-image", methods=["POST"])
    def api_generate_image():
        body = request.get_json()
        prompt = body.get("prompt", "").strip()
        style = body.get("style", "portrait")
        if not prompt:
            return jsonify({"ok": False, "error": "no prompt"}), 400

        configs = {
            "portrait": (512, 512, "professional headshot portrait, Chinese beauty industry, studio lighting, "),
            "banner": (1024, 512, "professional banner design, beauty salon brand, elegant, "),
            "poster": (768, 1024, "promotional poster, beauty wellness, luxury, "),
            "logo": (512, 512, "minimalist logo, beauty brand, clean, professional, "),
        }
        w, h, prefix = configs.get(style, configs["portrait"])
        full = prefix + prompt
        safe = urllib.parse.quote(full)
        url = f"/api/proxy-image?url={urllib.parse.quote(f'https://image.pollinations.ai/prompt/{safe}?width={w}&height={h}&nologo=true')}"

        return jsonify({"ok": True, "url": url, "prompt": full, "style": style})

    @app.route("/api/proxy-image")
    def api_proxy_image():
        raw = request.args.get("url", "")
        url = urllib.parse.unquote(raw)
        if not _is_pollinations_url(url):
            return "invalid", 400
        try:
            for _ in range(4):
                r = http_requests.get(url, timeout=30, allow_redirects=False, stream=True)
                if 300 <= r.status_code < 400:
                    next_url = urllib.parse.urljoin(url, r.headers.get("Location", ""))
                    r.close()
                    if not _is_pollinations_url(next_url):
                        return "invalid redirect", 400
                    url = next_url
                    continue
                content_type = r.headers.get("Content-Type", "")
                if not content_type.startswith("image/"):
                    return "invalid response", 502
                content = bytearray()
                for chunk in r.iter_content(64 * 1024):
                    content.extend(chunk)
                    if len(content) > 12 * 1024 * 1024:
                        return "image too large", 413
                return bytes(content), r.status_code, {"Content-Type": content_type}
            return "too many redirects", 502
        except:
            return "error", 500

    @app.route("/api/module7-images", methods=["POST"])
    def api_module7_images():
        """为模块7生成3张形象预览图"""
        body = request.get_json()
        cid = body.get("conversation_id", "")

        images = {}
        for name, prompt in [
            ("avatar", "professional WeChat avatar, Chinese female entrepreneur, confident, elegant, beauty industry"),
            ("cover", "WeChat cover banner, beauty salon brand, luxury, pink gold tones, elegant"),
            ("lifestyle", "lifestyle photo style, beauty and wellness, clean aesthetic, warm lighting"),
        ]:
            safe = urllib.parse.quote(prompt)
            inner = f"https://image.pollinations.ai/prompt/{safe}?width=512&height=512&nologo=true"
            outer = urllib.parse.quote(inner)
            images[name] = f"/api/proxy-image?url={outer}"

        return jsonify({"ok": True, "images": images})


def _is_pollinations_url(url):
    try:
        parsed = urllib.parse.urlparse(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "image.pollinations.ai"
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False
