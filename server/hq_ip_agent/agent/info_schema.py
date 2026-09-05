"""《黄雀 IP 人设定位采集表（模块 1-4 补全版）》的完整拆解。

把采集表 8 大模块、43 个问题逐条拆成「字段」：
- 每个字段有规范键（模块id.字段key）、问题原文（label）、是否核心（core）、
  以及它主要喂养报告模板里的哪个模块（feeds）。
- 主 Agent 通过 update_profile 把采集到的事实按规范键写入内部信息表，
  由 profile_status 按字段粒度回报「已采 / 缺失」，供 Agent 判断下一个问题
  和「信息是否足够出报告」。
- 代码不做正则校验、不按字段硬编码用户输入；字段表只是采集参考与覆盖度
  提示，真正的内容判断交给主 Agent。
"""

from __future__ import annotations

# 8 大模块，每个问题一个字段。
# key 命名：模块id.字段key；feeds 表示该字段主要供给报告模板的哪个部分。
MODULES = [
    {
        "id": "basic",
        "name": "基本信息",
        "feeds": "报告标题/模块一关键词、模块二人设",
        "fields": [
            {"key": "basic.name", "label": "姓名/昵称", "core": True},
            {"key": "basic.gender_age", "label": "性别/年龄", "core": False},
            {"key": "basic.city", "label": "所在城市", "core": False},
            {"key": "basic.phone", "label": "手机号（可选）", "core": False},
        ],
    },
    {
        "id": "career",
        "name": "职业背景",
        "feeds": "模块一关键词/市场机缘、模块二人设",
        "fields": [
            {"key": "career.current_job", "label": "当前职业/身份", "core": True},
            {"key": "career.years", "label": "从业年限", "core": True},
            {"key": "career.past_jobs", "label": "做过哪些行业/岗位", "core": False},
            {"key": "career.income_source", "label": "目前主要收入来源", "core": False},
            {"key": "career.income_range", "label": "年收入区间", "core": False},
        ],
    },
    {
        "id": "experience",
        "name": "核心经历",
        "feeds": "模块一关键词/潜在风险、模块四故事库",
        "fields": [
            {"key": "experience.setback", "label": "人生中最大的挫折/低谷", "core": True},
            {"key": "experience.proudest", "label": "最有成就感的一件事", "core": True},
            {"key": "experience.praised", "label": "被人夸最多的是什么", "core": False},
            {"key": "experience.criticized", "label": "被人吐槽最多的是什么", "core": True},
            {"key": "experience.strength", "label": "最厉害的能力", "core": True},
        ],
    },
    {
        "id": "direction",
        "name": "内容方向",
        "feeds": "模块一定位诊断、模块二人设",
        "fields": [
            {"key": "direction.track", "label": "想做的赛道", "core": True},
            {"key": "direction.audience", "label": "目标受众是谁", "core": True},
            {"key": "direction.pains", "label": "能帮他们解决什么问题", "core": True},
            {"key": "direction.differentiation", "label": "你的差异化是什么", "core": True},
            {"key": "direction.existing_accounts", "label": "目前有什么内容账号", "core": False},
            {"key": "direction.passion", "label": "长期痴迷/愿意长期投入的事", "core": False},
        ],
    },
    {
        "id": "style",
        "name": "性格与风格",
        "feeds": "模块二人设塑造、模块三价值主张",
        "fields": [
            {"key": "style.personality", "label": "3 个词形容性格", "core": True},
            {"key": "style.tone", "label": "说话风格偏好", "core": True},
            {"key": "style.disliked_style", "label": "特别讨厌的博主风格", "core": False},
            {"key": "style.posting_habit", "label": "朋友圈/聊天发内容的习惯", "core": False},
            {"key": "style.values", "label": "最重要的价值观和信念", "core": False},
            {"key": "style.audience_aspiration", "label": "目标用户希望成为什么样的人", "core": False},
        ],
    },
    {
        "id": "value",
        "name": "价值主张",
        "feeds": "模块三价值主张提炼、模块二人设金句",
        "fields": [
            {"key": "value.memorable_quote", "label": "最想让人记住的一句话", "core": True},
            {"key": "value.self_intro", "label": "一句话介绍自己", "core": True},
            {"key": "value.why_follow", "label": "客户/朋友为什么愿意跟着你", "core": True},
            {"key": "value.first_impression", "label": "希望给人的第一印象", "core": False},
            {"key": "value.longterm_influence", "label": "未来 3 年想形成的长期影响力", "core": False},
        ],
    },
    {
        "id": "story",
        "name": "故事资产",
        "feeds": "模块四故事库、模块一关键词",
        "fields": [
            {"key": "story.comeback", "label": "「绝境翻身」的故事", "core": True},
            {"key": "story.pit", "label": "「踩过大坑」的故事", "core": True},
            {"key": "story.rise", "label": "「逆袭成功」的故事", "core": True},
            {"key": "story.dramatic", "label": "奇葩/戏剧性的经历", "core": False},
            {"key": "story.team_project", "label": "带过团队/做过的项目", "core": False},
            {"key": "story.shared_struggle", "label": "和目标用户的相似经历/共鸣处境", "core": False},
            {"key": "story.recurring_theme", "label": "愿长期反复讲述的故事主题", "core": False},
        ],
    },
    {
        "id": "business",
        "name": "商业目标",
        "feeds": "模块三变现路径、模块四执行优先级",
        "fields": [
            {"key": "business.goal", "label": "做 IP 最想达到什么目的", "core": True},
            {"key": "business.time_budget", "label": "预计投入多少时间", "core": False},
            {"key": "business.products", "label": "现成的产品/服务", "core": True},
            {"key": "business.short_goal", "label": "短期目标（3 个月内）", "core": False},
            {"key": "business.long_goal", "label": "长期目标（1 年内）", "core": False},
        ],
    },
]

MODULE_IDS = [m["id"] for m in MODULES]


def all_fields() -> list[dict]:
    """全部字段（扁平），带模块名。"""
    out = []
    for m in MODULES:
        for f in m["fields"]:
            out.append({
                "key": f["key"],
                "label": f["label"],
                "core": f["core"],
                "module": m["id"],
                "module_name": m["name"],
            })
    return out


FIELDS = all_fields()
FIELD_BY_KEY = {f["key"]: f for f in FIELDS}
CORE_FIELDS = [f for f in FIELDS if f["core"]]


def core_ratio(covered_keys: set[str]) -> float:
    """核心字段覆盖率（0.0~1.0），供状态展示与 Agent 判断参考。"""
    if not CORE_FIELDS:
        return 1.0
    n = sum(1 for f in CORE_FIELDS if f["key"] in covered_keys)
    return n / len(CORE_FIELDS)


def module_field_keys(module_id: str) -> list[str]:
    return [f["key"] for f in FIELDS if f["module"] == module_id]
