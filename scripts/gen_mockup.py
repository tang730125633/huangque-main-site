#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
黄雀 AI · 网站模块页高保真稿生成器 (gpt-image-2)

用现有对标稿当视觉参照(edits 接口),把已建但缺稿的工作台模块页
(视频/音频/编导/画布/资产/成本) 出成同一套暗色"作战台+黄雀金"风格的高保真 mockup。

用法:
  python3 scripts/gen_mockup.py video            # 出单页
  python3 scripts/gen_mockup.py video audio       # 出多页
  python3 scripts/gen_mockup.py all               # 全部
  python3 scripts/gen_mockup.py --list            # 列出可出的页面

凭证: 从 config.local.env 读 OPENAI_API_KEY (gitignore, 绝不进 git)。
输出: docs/黄雀网站主站图预览（对标）/_模块页补图/<中文名>.png
"""
import os, sys, base64, pathlib, json, urllib.request, urllib.error

ROOT = pathlib.Path(__file__).resolve().parent.parent
REF_DIR = ROOT / "docs" / "黄雀网站主站图预览（对标）"
OUT_DIR = REF_DIR / "_模块页补图"
MODEL = "gpt-image-2"
SIZE = "1536x1024"   # 横向桌面端，近 3:2
QUALITY = "high"

# ---- 读 key（只从本地 gitignore 文件 / 环境变量，绝不硬编码） ----
def load_key():
    k = os.environ.get("OPENAI_API_KEY")
    if k:
        return k.strip()
    envf = ROOT / "config.local.env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            line = line.strip()
            if line.startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("✗ 找不到 OPENAI_API_KEY（config.local.env 或环境变量）")

# ---- 共用设计语言(品牌锚) ----
SHARED = (
    "This is one screen of a cohesive product. KEEP the EXACT same visual language as the "
    "reference image: dark 'operating desk' UI, near-black deep-navy background (#050811~#0b1018) "
    "with a faint radial glow, gold primary accent (#e7b24c) used for the logo / key numbers / the "
    "single primary CTA button, cyan secondary accent (#35d6c6) for links and minor highlights, "
    "low-saturation green/amber/red status colors, monospace tabular numerals for all numbers/points/IDs, "
    "clean sans-serif Chinese text, card-based modular panels with subtle 1px borders, soft inner glow, "
    "12-18px rounded corners, generous spacing, data-dense yet elegant. "
    "KEEP the left narrow vertical icon nav rail (items: 今日 获客 作图 视频 资产 成本) with the current "
    "page highlighted in gold with a gold left accent bar. "
    "KEEP the top bar: gold sparrow logo + '黄雀 AI', current client chip '仙颜美容 · 示例', an online status "
    "'34 个 Bot 在线' with a green dot, and a gold monospace points balance '340'. "
    "Chinese-language interface, realistic, professional, Dribbble/Behance quality, high-fidelity desktop "
    "web UI mockup, single full screen, 16:9 desktop browser. Render Chinese text as cleanly as possible."
)

# ---- 每页：参照稿 + 改造指令 ----
PAGES = {
    "video": {
        "name": "视频-数字人口播页",
        "ref": "Ai工作台页面.png",
        "prompt": (
            "Redesign this three-column creation screen into the 【视频 · 数字人口播】 page. "
            "LEFT: the same icon nav rail but with '视频' highlighted in gold. "
            "CENTER creation panel titled '数字人口播': a 9:16 portrait photo upload zone labeled '上传正脸图', "
            "a '口播脚本' multiline text box with sample beauty-industry script, a segmented control with three "
            "pills '形象驱动 5点 / 声音驱动 4点 / 口型同步 4点', selectors for 分辨率(1080p) · 比例(9:16竖版) · "
            "音色(Paul 男声), and a full-width gold primary button '生成口播 (13 点)'. "
            "RIGHT result inspector: a vertical 9:16 video frame showing a realistic Chinese beauty-clinic "
            "female presenter speaking to camera (a digital-human host still), with play/scrubber controls, an "
            "action row of icon buttons '重新生成 · 继续改 · 发飞书 · 下载 · 入库', and a '最近口播' row of small "
            "vertical-video thumbnails. " + SHARED
        ),
    },
    "audio": {
        "name": "音频-配音页",
        "ref": "Ai工作台页面.png",
        "prompt": (
            "Redesign this three-column creation screen into the 【音频 · 配音】 page. "
            "LEFT nav rail with '资产' area — but actually highlight an 音频 entry; keep rail consistent. "
            "CENTER creation panel titled '配音音频': a '配音文案' text box, a voice library as small selectable "
            "cards (大鹏 IVC / 泽龙 IVC / Paul 男声) with tiny waveforms, a segmented '语速：偏慢 / 正常 / 偏快', a "
            "segmented '情绪：自然 / 热情 / 温柔', and a full-width gold button '生成配音 (4 点)'. "
            "RIGHT inspector: a large gold audio WAVEFORM with a play head, a takes list '试听 · 时长 · 大小', and a "
            "'最近音频' list. " + SHARED
        ),
    },
    "script": {
        "name": "编导-脚本页",
        "ref": "Ai工作台页面.png",
        "prompt": (
            "Redesign this three-column creation screen into the 【编导 · 文案脚本】 page. "
            "LEFT nav rail consistent (highlight 编导/作图 area in gold). "
            "CENTER creation panel titled '文案编导': a '选题/卖点' input, segmented '风格：口播 / 剧情 / 种草', "
            "segmented '时长：15s / 30s / 60s', platform chips '抖音 · 小红书 · 视频号', and a gold button "
            "'生成脚本 (3 点)'. "
            "RIGHT inspector: a generated storyboard as stacked cards each with 镜号 / 画面描述 / 口播文案 / 时长, "
            "plus quick-convert buttons '转作图 · 转口播'. " + SHARED
        ),
    },
    "canvas": {
        "name": "画布-海报编辑页",
        "ref": "Ai工作台页面.png",
        "prompt": (
            "Redesign this screen into the 【画布 · 海报编辑】 page — a freeform creative canvas editor. "
            "LEFT nav rail consistent (highlight 作图 in gold). "
            "CENTER: a large editing CANVAS showing a beauty-clinic promotional poster being composited "
            "(headline 科技焕肤, a model photo, price tag, brand mark), with a floating toolbar (选择/文字/图形/裁剪/"
            "AI 重绘) and selection handles on a layer. "
            "RIGHT: a '图层' panel (list of layers with eye toggles) above a '属性' panel (位置/尺寸/字体/颜色), "
            "and a gold button '导出 · 入库'. " + SHARED
        ),
    },
    "assets": {
        "name": "资产库页",
        "ref": "获客结果页.png",
        "prompt": (
            "Redesign this screen into the 【资产库】 page — an asset library, NOT a table of leads. "
            "LEFT nav rail with '资产' highlighted in gold. "
            "TOP: a search box + filter chips '全部 / 图片 / 海报 / 视频 / 数字人 / 音频' + small stats "
            "(素材 · 作品 · 交付). "
            "MAIN: a dense responsive THUMBNAIL GRID of real-looking assets (beauty posters, digital-human video "
            "stills, 小红书 covers, audio cards), each card showing a type badge, source, and the linked task; "
            "hover-style action overlay on one card (预览 / 下载 / 发飞书 / 删除). " + SHARED
        ),
    },
    "cost": {
        "name": "成本账本页",
        "ref": "今日dashborad的页面.png",
        "prompt": (
            "Redesign this dashboard into the 【成本账本】 page — cost & margin observability. "
            "LEFT nav rail with '成本' highlighted in gold. "
            "TOP: four KPI stat cards with gold monospace numbers — 今日成本 ¥ / 本月成本 ¥ / 毛利率 % / 失败退点. "
            "MIDDLE: a '成本趋势' line/area chart (gold + cyan lines) and a '按能力拆解' donut/bars "
            "(作图 / 口播 / 配音 / 编导). "
            "BOTTOM: a '消费明细' table (时间 / 项目 / 类型 / 模型 / 消耗点数 / 金额 / 状态) with a few rows marked "
            "失败-已退点 in low-saturation red, monospace numerals, sticky header. " + SHARED
        ),
    },

    # ---- 第二批：已建缺稿 + 运营骨干页 + 对外营销页 ----
    "inspiration": {
        "name": "灵感案例页",
        "ref": "_模块页补图/资产库页.png",
        "prompt": (
            "Redesign this screen into the 【灵感案例】 page — an inspiration gallery to '做同款'. "
            "LEFT nav rail with '灵感' highlighted in gold at the very top. "
            "TOP: a row of category filter chips (全部 / 美业海报 / 数字人口播 / 小红书封面 / 节日营销 / 活动促销) and a "
            "search box. "
            "MAIN: a MASONRY (Pinterest-style, varied heights) grid of real-looking beauty-industry creative cards "
            "(posters, video covers, 小红书 covers); each card shows a small category tag, a like/收藏 count in "
            "monospace, and a hover-style gold button '做同款' on one highlighted card. " + SHARED
        ),
    },
    "clients": {
        "name": "客户管理页",
        "ref": "获客结果页.png",
        "prompt": (
            "Redesign this screen into the 【客户管理】 page — manage multiple client accounts (the page behind the "
            "top-bar client switcher). LEFT nav rail consistent. "
            "TOP: stat chips (客户总数 / 活跃客户 / 本月新增 / 待绑定群) + a '新增客户' gold button + search. "
            "MAIN: a CLIENT TABLE, each row = one client with: 头像+名称 (e.g. 仙颜美容 / 韩辰皮肤管理 / 知妍医美), "
            "行业标签, 绑定的飞书群 (a 飞书 group chip with a green '已绑定' or amber '待绑定' status), 剩余点数 "
            "(gold monospace), 本月消耗, 负责人, 状态, and a row action menu. A right-side drawer/inspector preview "
            "shows one client's detail (飞书群绑定 / 点数分配 / 最近任务). " + SHARED
        ),
    },
    "bots": {
        "name": "飞书Bot矩阵页",
        "ref": "今日dashborad的页面.png",
        "prompt": (
            "Redesign this dashboard into the 【飞书 Bot 矩阵】 page — the real '34 个 Bot 在线' fleet view. "
            "LEFT nav rail consistent. "
            "TOP: stat cards — 在线 Bot 34 / 离线 2 / 今日消息 / 平均响应. "
            "MAIN: a responsive GRID of BOT CARDS, each card = one 飞书 bot with: bot 头像+名称 (小秋 / 小夏 / 小婷 …), "
            "a green online dot or grey offline dot, the 绑定客户群 it serves, 今日任务数, 最近活跃时间 (monospace), and "
            "small action icons (重启 / 解绑 / 日志). Two cards show amber '离线' state in low saturation. " + SHARED
        ),
    },
    "tasks": {
        "name": "任务中心页",
        "ref": "获客结果页.png",
        "prompt": (
            "Redesign this screen into the 【任务中心】 page — a cross-module task queue. LEFT nav rail consistent. "
            "TOP: status filter segmented (全部 / 进行中 / 排队 / 已完成 / 失败) + counts, and stat chips "
            "(进行中 / 今日完成 / 失败率 / 平均耗时). "
            "MAIN: a TASK TABLE, each row: 任务ID (monospace) / 类型 (作图·口播·配音·抓取 with a colored tag) / 客户 / "
            "状态 (a pill: 进行中=cyan, 排队=grey, 完成=green, 失败=low-sat red) / 进度 (a thin progress bar) / 消耗点数 / "
            "耗时 / 操作 (重试 on failed rows). A couple of failed rows show '失败-已退点'. " + SHARED
        ),
    },
    "settings": {
        "name": "设置-团队页",
        "ref": "充值页面.png",
        "prompt": (
            "Redesign this screen into the 【设置 · 团队】 page. LEFT nav rail consistent. "
            "Use a two-column settings layout: a LEFT section menu (账号资料 / 团队成员 / 安全 · 口令 / 飞书绑定 / 计费) "
            "with '团队成员' active in gold, and a RIGHT content area showing: an 账号资料 card (头像/名称/角色/邮箱), then a "
            "'团队成员' TABLE (成员 / 角色: 管理员·运营·查看 / 负责客户 / 最近登录 / 操作), and a '团队口令登录' card with a "
            "masked 口令 field and a gold '生成新口令' button. Clean, trustworthy, settings-grade. " + SHARED
        ),
    },
    "pricing": {
        "name": "营销-价格套餐页",
        "ref": "image.png",
        "prompt": (
            "Redesign this landing page into the public 【价格 / 套餐】 marketing page (NOT the in-app points recharge). "
            "KEEP the marketing top nav (gold sparrow logo + '黄雀 AI'; menu 产品 / 解决方案 / 价格 / 关于; right side "
            "'登录' + a gold '预约演示' button) and KEEP the footer (公司名 + 备案号 粤ICP备2025447525号-2). "
            "HERO: a centered headline '选择适合你的方案' + subline + a 月付/年付 toggle. "
            "MAIN: a row of 3-4 PRICING PLAN CARDS — 基础版 / 专业版(金色描边, 标'推荐') / 团队版 / 企业版(联系销售), each with "
            "price in gold monospace, a feature list with check marks, and a CTA button. BELOW: a feature-comparison "
            "table (功能 × 套餐) and a short FAQ. Dark operating-desk style with gold. " + SHARED
        ),
    },
    "solutions": {
        "name": "营销-解决方案页",
        "ref": "image.png",
        "prompt": (
            "Redesign this landing page into the 【解决方案】 marketing page. KEEP the marketing top nav (logo + 产品 / "
            "解决方案 / 价格 / 关于 + 登录 + gold '预约演示') and the footer with 备案号 粤ICP备2025447525号-2. "
            "HERO: '为你的行业，定制获客与内容方案'. "
            "MAIN: FOUR industry solution blocks — 美业 / 电商 / IP 赋能 / 品牌孵化 — each a card with an icon, the pain "
            "point, the 黄雀 workflow (评论区获客 → AI 内容 → 飞书成交), and a mini result stat. Include a real-metrics band "
            "(1,770+ 评论采集 · 3,168 账号线索池 · 100+ 精准名单 · 80% 回复率) and a bottom CTA '预约演示'. " + SHARED
        ),
    },
    "about": {
        "name": "营销-关于页",
        "ref": "image.png",
        "prompt": (
            "Redesign this landing page into the 【关于黄雀】 marketing/about page. KEEP the marketing top nav (logo + "
            "产品 / 解决方案 / 价格 / 关于 + 登录 + gold '预约演示') and the footer with 公司名 + 备案号 粤ICP备2025447525号-2. "
            "SECTIONS: a brand-story hero (品牌名出自「螳螂捕蝉，黄雀在后」— 精准、敏锐、后发制人的捕手; gold sparrow motif), a "
            "'我们做什么' two-line value prop (评论区获客，AI 内容成交), a 愿景/团队 section, a 资质与背书 row (公司全称 广州黄雀传媒 "
            "有限公司 + ICP 备案), and a 联系/预约 CTA block. Elegant, credible, dark + gold. " + SHARED
        ),
    },
}

def gen(page_key, api_key):
    spec = PAGES[page_key]
    ref_path = REF_DIR / spec["ref"]
    if not ref_path.exists():
        print(f"  ✗ 参照稿不存在: {ref_path}")
        return False
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{spec['name']}.png"

    # multipart/form-data 手搓(避免第三方依赖)
    boundary = "----huangqueboundary7e3f"
    img_bytes = ref_path.read_bytes()
    parts = []
    def field(name, value):
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(f"{value}\r\n".encode())
    field("model", MODEL)
    field("size", SIZE)
    field("quality", QUALITY)
    field("n", "1")
    field("prompt", spec["prompt"])
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(f'Content-Disposition: form-data; name="image"; filename="{spec["ref"]}"\r\n'.encode())
    parts.append(b"Content-Type: image/png\r\n\r\n")
    parts.append(img_bytes)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(
        "https://api.openai.com/v1/images/edits",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    print(f"  → 出图中 [{page_key}] {spec['name']} (参照 {spec['ref']}) …")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  ✗ HTTP {e.code}: {e.read().decode()[:400]}")
        return False
    b64 = data["data"][0]["b64_json"]
    out_path.write_bytes(base64.b64decode(b64))
    print(f"  ✓ 已保存: {out_path}")
    return True

def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__); return
    if args[0] == "--list":
        for k, v in PAGES.items():
            print(f"  {k:8} → {v['name']}  (参照 {v['ref']})")
        return
    keys = list(PAGES) if args[0] == "all" else args
    api_key = load_key()
    ok = 0
    for k in keys:
        if k not in PAGES:
            print(f"  ✗ 未知页面: {k}（--list 看可选）"); continue
        if gen(k, api_key):
            ok += 1
    print(f"\n完成 {ok}/{len(keys)} 张 → {OUT_DIR}")

if __name__ == "__main__":
    main()
