"""视频脚本服务模块"""
from flask import request, jsonify, render_template

def register_video_routes(app):

    @app.route("/videos")
    def videos_page():
        return render_template("videos.html")

    @app.route("/api/module8-video", methods=["POST"])
    def api_module8_video():
        import urllib.parse
        body = request.get_json()
        topic = body.get("topic", "美业拓客").strip()

        videos = []
        styles = [
            ("短视频口播", "single person facing camera, confident professional Chinese female entrepreneur, clean background, soft lighting, douyin vertical format"),
            ("产品展示", "product closeup shot, slow rotation display, soft lighting, luxury skincare style, xiaohongshu format"),
            ("场景故事", "beauty salon interior warm scene, customer interaction, natural light, cinematic storytelling style"),
        ]
        for i, (name, desc) in enumerate(styles):
            safe = urllib.parse.quote(f"{desc}, {topic}, high quality, cinematic lighting")
            inner = f"https://image.pollinations.ai/prompt/{safe}?width=768&height=1024&nologo=true"
            outer = urllib.parse.quote(inner)
            preview_url = f"/api/proxy-image?url={outer}"

            script_lines = []
            script_lines.append(f"【场景{i+1}】{name}")
            script_lines.append("")
            script_lines.append("前3秒（钩子）：制造悬念或痛点")
            script_lines.append(f"  - 你还在为{topic}发愁吗？")
            script_lines.append("")
            script_lines.append("中间（干货）：给出方法或观点")
            script_lines.append(f"  - 今天教你一个{topic}的绝招")
            script_lines.append("")
            script_lines.append("结尾（行动号召）：")
            script_lines.append("  - 关注我，下期更精彩")
            script_lines.append(f"时长：30-60秒 | 平台：抖音/小红书/视频号")

            videos.append({
                "name": f"{i+1}. {name}",
                "preview": preview_url,
                "prompt": f"{desc}, {topic}, high quality",
                "tools": ["即梦 (jimeng.jianying.com)", "可灵 (klingai.com)"],
                "script": "\n".join(script_lines)
            })

        return jsonify({"ok": True, "videos": videos})
