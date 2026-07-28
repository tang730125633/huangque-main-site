"""技能扩展层 - 模块1-12全覆盖"""
from flask import request, jsonify, render_template
import urllib.parse

def register_skills(app):

    # ═══ 模块1: 竞品定位对比 ═══
    @app.route("/api/m1-competitor", methods=["POST"])
    def api_m1_competitor():
        from model_router import call_ai
        body = request.get_json()
        niche = body.get("niche", "美业").strip()
        keywords = body.get("keywords", "").strip()
        if not keywords: return jsonify({"ok":False,"error":"请输入关键词"}),400

        prompt = f"""分析「{niche}」赛道，关键词：{keywords}。
请输出：
1. 🔍 赛道内3个典型IP的定位公式（我是[谁]，专为[谁]解决[什么]）
2. 📊 他们的优势/劣势/差异化缺口
3. 🎯 基于缺口，给出3个你可以切入的定位方向
4. ⚠️ 哪些定位已经被做烂了（红海警告）
直接输出Markdown。"""
        try:
            r = call_ai([{"role":"user","content":prompt}], False, 0.7)
            return jsonify({"ok":True,"report":r.json()["choices"][0]["message"]["content"]})
        except Exception as e: return jsonify({"ok":False,"error":str(e)}),500

    # ═══ 模块2: 人设卡片 ═══
    @app.route("/api/m2-persona", methods=["POST"])
    def api_m2_persona():
        from model_router import call_ai
        body = request.get_json()
        profile = body.get("profile", "").strip()
        if not profile: return jsonify({"ok":False,"error":"请描述你的基本情况"}),400

        prompt = f"""基于以下信息生成「IP人设卡片」：

{profile}

输出包含：
1. 🏷️ 人设标签（3-5个关键词）
2. 🎭 台上形象（观众看到的你）
3. 🏠 台下真实（真实的你）
4. 💬 口头禅/金句（3句）
5. 📸 镜头状态建议
6. ⚡ 差异化记忆点

直接输出Markdown格式的人设卡片。"""
        try:
            r = call_ai([{"role":"user","content":prompt}], False, 0.7)
            return jsonify({"ok":True,"report":r.json()["choices"][0]["message"]["content"]})
        except Exception as e: return jsonify({"ok":False,"error":str(e)}),500

    # ═══ 模块3: 价值金字塔 ═══
    @app.route("/api/m3-value", methods=["POST"])
    def api_m3_value():
        from model_router import call_ai
        body = request.get_json()
        niche = body.get("niche", "美业").strip()
        advantage = body.get("advantage", "").strip()
        if not advantage: return jsonify({"ok":False,"error":"请描述你的核心优势"}),400

        prompt = f"""为「{niche}」赛道构建价值金字塔。

核心优势：{advantage}

输出层级结构：
🔺 塔尖：核心价值主张（一句话，<15字）
🔺 第二层：3个信任状（证书/案例/数据）
🔺 第三层：5个差异化卖点
🔺 底座：目标客户的5个核心痛点

直接输出Markdown。"""
        try:
            r = call_ai([{"role":"user","content":prompt}], False, 0.7)
            return jsonify({"ok":True,"report":r.json()["choices"][0]["message"]["content"]})
        except Exception as e: return jsonify({"ok":False,"error":str(e)}),500

    # ═══ 模块4: 故事框架 ═══
    @app.route("/api/m4-story", methods=["POST"])
    def api_m4_story():
        from model_router import call_ai
        body = request.get_json()
        experience = body.get("experience", "").strip()
        if not experience: return jsonify({"ok":False,"error":"请描述你的经历"}),400

        prompt = f"""把以下经历提炼成可复用的「故事资产」：

{experience}

为每个故事输出：
1. 📖 故事名称
2. 🎯 适用场景（什么时候讲）
3. 📝 故事框架（Hero's Journey结构：平凡世界→冒险召唤→拒绝→导师→跨越→回归）
4. 💥 金句提取（故事里最有杀伤力的一句话）
5. 🎬 讲述建议（语气、节奏、时长）

生成3个故事版本。直接输出Markdown。"""
        try:
            r = call_ai([{"role":"user","content":prompt}], False, 0.7)
            return jsonify({"ok":True,"report":r.json()["choices"][0]["message"]["content"]})
        except Exception as e: return jsonify({"ok":False,"error":str(e)}),500

    # ═══ 模块5: Hook生成器 ═══
    @app.route("/api/m5-hooks", methods=["POST"])
    def api_m5_hooks():
        from model_router import call_ai
        body = request.get_json()
        topic = body.get("topic", "").strip()
        if not topic: return jsonify({"ok":False,"error":"请输入选题"}),400

        prompt = f"""选题：{topic}

为这个选题生成10个Hook（钩子），每类2个：
1. 🔥 痛点型：直接扎心
2. 🤯 反常识型：颠覆认知
3. 📖 故事型：勾起好奇
4. 💰 利益型：明确好处
5. ⏰ 紧迫型：制造稀缺

每个Hook < 30字。标注适合的平台（抖音/小红书/朋友圈/视频号）。
直接输出。"""
        try:
            r = call_ai([{"role":"user","content":prompt}], False, 0.8)
            return jsonify({"ok":True,"report":r.json()["choices"][0]["message"]["content"]})
        except Exception as e: return jsonify({"ok":False,"error":str(e)}),500

    # ═══ 模块9: 私域漏斗 ═══
    @app.route("/api/m9-funnel", methods=["POST"])
    def api_m9_funnel():
        from model_router import call_ai
        body = request.get_json()
        business = body.get("business", "美业").strip()
        product = body.get("product", "").strip()
        if not product: return jsonify({"ok":False,"error":"请描述你的产品/服务"}),400

        prompt = f"""为「{business}」设计私域转化漏斗。

产品：{product}

输出：
1. 🔽 漏斗结构（展示→兴趣→信任→成交→裂变）
2. 📱 每个阶段的具体触点（朋友圈/私信/群/视频号）
3. 🤖 自动欢迎语SOP（3段式：破冰→挖掘需求→引导下一步）
4. 🏷️ 标签体系（至少8个标签维度）
5. 📅 7天新粉转化SOP（每天做什么，说什么）

直接输出Markdown。"""
        try:
            r = call_ai([{"role":"user","content":prompt}], False, 0.7)
            return jsonify({"ok":True,"report":r.json()["choices"][0]["message"]["content"]})
        except Exception as e: return jsonify({"ok":False,"error":str(e)}),500

    # ═══ 模块11: 成交话术 ═══
    @app.route("/api/m11-sales", methods=["POST"])
    def api_m11_sales():
        from model_router import call_ai
        body = request.get_json()
        product = body.get("product", "").strip()
        price = body.get("price", "").strip()
        if not product: return jsonify({"ok":False,"error":"请描述产品"}),400

        prompt = f"""产品：{product}
价格：{price if price else '未指定'}

生成完整成交话术包：
1. 🎯 破冰话术（3句，让对方愿意聊下去）
2. 🔍 需求挖掘（5个精准提问）
3. 💎 价值塑造（3个角度展示产品价值）
4. 🛡️ 异议处理（最常见的5个拒绝理由+回应）
5. 💰 逼单话术（限时/限量/稀缺，3个版本）
6. 🔄 跟进话术（被拒绝后的3次跟进模板）

直接输出Markdown。"""
        try:
            r = call_ai([{"role":"user","content":prompt}], False, 0.7)
            return jsonify({"ok":True,"report":r.json()["choices"][0]["message"]["content"]})
        except Exception as e: return jsonify({"ok":False,"error":str(e)}),500

    # ═══ 模块12: 公众号日历 ═══
    @app.route("/api/m12-calendar", methods=["POST"])
    def api_m12_calendar():
        from model_router import call_ai
        body = request.get_json()
        niche = body.get("niche", "美业").strip()
        keywords = body.get("keywords", "").strip()
        if not keywords: return jsonify({"ok":False,"error":"请输入关键词"}),400

        prompt = f"""赛道：{niche} | 关键词：{keywords}

生成公众号内容方案：
1. 📅 30天内容日历（标题+一句话概要+封面建议）
2. 🔍 SEO标题优化（5个高搜索量标题方案）
3. 📊 内容配比（干货:故事:成交 = 5:3:2）
4. 🔗 文章互链策略（如何让一篇带火另一篇）
5. 💰 变现路径（文章→私域→成交的完整链路）

直接输出Markdown表格。"""
        try:
            r = call_ai([{"role":"user","content":prompt}], False, 0.7)
            return jsonify({"ok":True,"report":r.json()["choices"][0]["message"]["content"]})
        except Exception as e: return jsonify({"ok":False,"error":str(e)}),500

    # ═══ 跨模块: 受众画像 ═══
    @app.route("/api/audience", methods=["POST"])
    def api_audience():
        from model_router import call_ai
        body = request.get_json()
        niche = body.get("niche", "美业").strip()
        if not niche: return jsonify({"ok":False,"error":"请输入赛道"}),400

        prompt = f"""为「{niche}」赛道构建3类典型受众画像：

每类包含：
- 👤 姓名/年龄/职业/收入
- 😤 核心痛点（3个）
- 🎯 内容偏好（喜欢看什么）
- 📱 活跃平台（哪个App，什么时间）
- 💰 付费意愿（什么时候愿意掏钱）
- 🚫 信任障碍（为什么不相信你）

直接输出Markdown。"""
        try:
            r = call_ai([{"role":"user","content":prompt}], False, 0.7)
            return jsonify({"ok":True,"report":r.json()["choices"][0]["message"]["content"]})
        except Exception as e: return jsonify({"ok":False,"error":str(e)}),500

    # ═══ 跨模块: 内容一鱼多吃 ═══
    @app.route("/api/repurpose", methods=["POST"])
    def api_repurpose():
        from model_router import call_ai
        body = request.get_json()
        content = body.get("content", "").strip()
        if not content: return jsonify({"ok":False,"error":"请输入原始内容"}),400

        prompt = f"""把以下内容「一鱼多吃」成5种格式：

原始内容：{content[:2000]}

输出：
1. 📱 朋友圈版（3条，不同角度）
2. 🎬 短视频口播版（1条，<60秒）
3. 📝 公众号长文版（大纲+框架）
4. 💬 私信话术版（用来私聊激活客户）
5. 📊 小红书图文版（标题+正文+话题标签）

直接输出。"""
        try:
            r = call_ai([{"role":"user","content":prompt}], False, 0.7)
            return jsonify({"ok":True,"report":r.json()["choices"][0]["message"]["content"]})
        except Exception as e: return jsonify({"ok":False,"error":str(e)}),500

    # ═══ 跨模块: 竞品IP拆解 ═══
    @app.route("/api/competitor-ip", methods=["POST"])
    def api_competitor_ip():
        from model_router import call_ai
        body = request.get_json()
        name = body.get("name", "").strip()
        if not name: return jsonify({"ok":False,"error":"请输入竞品IP名称"}),400

        prompt = f"""拆解竞品IP：「{name}」

从6个维度分析：
1. 🎯 定位公式
2. 🎭 人设标签
3. 📝 内容策略（选题类型/频率/平台）
4. 💬 话术特点（口头禅/常用句式）
5. 💰 变现模式（怎么赚钱的）
6. 🕳️ 弱点/可复制点（你可以学什么，他缺什么）

直接输出Markdown。"""
        try:
            r = call_ai([{"role":"user","content":prompt}], False, 0.7)
            return jsonify({"ok":True,"report":r.json()["choices"][0]["message"]["content"]})
        except Exception as e: return jsonify({"ok":False,"error":str(e)}),500

    # ═══ Skills page ═══
    @app.route("/skills")
    def skills_page():
        return render_template("skills.html")
