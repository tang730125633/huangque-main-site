"""Versioned in-process Skill contracts for the IP12 coaching pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import hashlib
import json
import os
import re


LEGACY_PIPELINE = "legacy"
SKILL_PIPELINE_V1 = "ip12-skills-v1"
PIPELINE_ENV = "HERMES_IP12_SKILL_PIPELINE_DEFAULT"


@dataclass(frozen=True)
class SkillSpec:
    skill_id: str
    contract_version: str
    prompt_version: str | None
    module: int | None
    allowed_statuses: tuple[str, ...]
    input_projection: str
    output_schema: str
    semantic_validator: str
    trace_id: str
    model_required: bool = True


SKILL_REGISTRY = {
    "intake": SkillSpec("intake", "2.0.0", "intake-v3", None,
                        ("collecting", "editing", "awaiting_confirmation"),
                        "confirmed-profile-v1", "ip12-turn-decision-v1", "reducer-v2", "intake"),
    "module_1_positioning": SkillSpec("module_1_positioning", "1.0.0", "module-1-positioning-v1", 1,
                                       ("active",), "confirmed-profile-v1", "ip12-turn-decision-v1",
                                       "reducer-v2", "module_1_positioning"),
    "module_2_persona": SkillSpec("module_2_persona", "1.0.0", "module-2-persona-v1", 2,
                                   ("active",), "confirmed-profile-v1", "ip12-turn-decision-v1",
                                   "reducer-v2", "module_2_persona"),
    "module_3_value": SkillSpec("module_3_value", "1.0.0", "module-3-value-v1", 3,
                                 ("active",), "confirmed-profile-v1", "ip12-turn-decision-v1",
                                 "reducer-v2", "module_3_value"),
    "module_4_story": SkillSpec("module_4_story", "1.0.0", "module-4-story-v1", 4,
                                 ("active",), "confirmed-profile-v1", "ip12-turn-decision-v1",
                                 "reducer-v2", "module_4_story"),
    "foundation_pdf": SkillSpec("foundation_pdf", "1.0.0", None, None, ("generating",),
                                 "frozen-snapshot-v1", "foundation-markdown-v1",
                                 "foundation-snapshot-v1", "foundation_pdf", model_required=False),
}

MODULE_SKILL_IDS = {
    1: "module_1_positioning",
    2: "module_2_persona",
    3: "module_3_value",
    4: "module_4_story",
}

MODULE_NAMES = {
    1: "定位诊断",
    2: "人设塑造",
    3: "价值主张提炼",
    4: "故事资产挖掘",
}

PRIVATE_INTAKE_FIELDS = {"age", "gender", "mobile", "income", "income_source", "income_range"}
FACT_LABELS = {
    "preferred_name": "姓名或昵称", "current_identity": "当前职业或身份",
    "city": "所在城市",
    "experience_years": "从业年限", "previous_work_experience": "过往行业或岗位",
    "biggest_setback": "最大挫折或低谷", "biggest_achievement": "最有成就感的事",
    "most_praised": "被夸最多的特点", "most_criticized": "被吐槽最多的特点",
    "core_skill_1": "第一项核心能力", "core_skill_2": "第二项核心能力",
    "niche": "内容赛道", "target_audience": "目标受众", "help_goal": "希望解决的问题",
    "differentiation": "差异化", "content_account": "现有内容账号",
    "personality_traits": "性格关键词", "tone_preference": "表达风格偏好",
    "disliked_style": "反感的表达风格", "content_habits": "内容习惯",
    "memorable_line": "希望被记住的一句话", "self_intro": "一句话自我介绍",
    "trust_reason": "信任原因", "story_comeback": "绝境翻身故事",
    "story_pitfall": "踩坑故事", "story_success": "成功转折故事",
    "story_unusual": "戏剧性经历", "team_project_experience": "团队或项目经历",
    "business_goal": "做 IP 的目的", "time_budget": "预计投入时间",
    "offer": "现有或计划产品服务", "three_month_goal": "三个月目标",
    "one_year_goal": "一年目标", "long_term_interest": "长期兴趣",
    "primary_platform": "主要发布平台", "desired_action": "希望用户采取的动作",
}


def normalize_pipeline_version(value):
    version = str(value or "").strip()
    if version == "v1":
        return SKILL_PIPELINE_V1
    return version if version in {LEGACY_PIPELINE, SKILL_PIPELINE_V1} else LEGACY_PIPELINE


def default_pipeline_version():
    return normalize_pipeline_version(os.environ.get(PIPELINE_ENV) or LEGACY_PIPELINE)


def project_pipeline_version(project):
    if not isinstance(project, dict):
        return LEGACY_PIPELINE
    return normalize_pipeline_version(
        project.get("pipeline_version")
        or (project.get("coach_state") or {}).get("pipeline_version")
    )


def skill_for_state(state):
    intake = (state or {}).get("intake") or {}
    if intake.get("status") != "complete":
        return SKILL_REGISTRY["intake"]
    module = int((state or {}).get("current_module") or 1)
    skill_id = MODULE_SKILL_IDS.get(module)
    return SKILL_REGISTRY.get(skill_id) if skill_id else None


def decorate_prompt(base_prompt, state):
    spec = skill_for_state(state)
    if spec is None:
        return str(base_prompt or "")
    return (
        str(base_prompt or "")
        + "\n\n当前 Skill 合同：%s@%s，Prompt=%s。"
          "Skill 只提出结构化候选，不得推进状态、确认用户选择或生成 PDF。"
        % (spec.skill_id, spec.contract_version, spec.prompt_version or "none")
    )


def confirmed_input_projection(state):
    """Project only confirmed facts and selected module results for a Skill call."""
    profile = deepcopy((state or {}).get("ip_profile") or {})
    outputs = {}
    for key, item in (profile.get("confirmed_outputs") or {}).items():
        if not isinstance(item, dict):
            continue
        clean = deepcopy(item)
        clean.pop("choice_snapshot", None)
        clean.pop("report_payload", None)
        outputs[str(key)] = clean
    return {
        "facts": deepcopy(profile.get("facts") or {}),
        "preferences": deepcopy(profile.get("preferences") or {}),
        "confirmed_outputs": outputs,
        "completed_modules": list((state or {}).get("completed_modules") or []),
    }


def project_skill_input(spec, state):
    if spec.input_projection != "confirmed-profile-v1":
        raise ValueError("当前 Skill 不接受模型输入投影")
    return confirmed_input_projection(state)


def validate_skill_output(spec, state, decision):
    """Fail closed before the unique Reducer performs semantic state validation."""
    if not spec.model_required or spec.semantic_validator != "reducer-v2":
        raise ValueError("当前 Skill 不接受模型输出")
    if not isinstance(decision, dict):
        raise ValueError("Skill 输出必须是结构化对象")
    if spec.module is None:
        status = str(((state or {}).get("intake") or {}).get("status") or "")
    else:
        if int((state or {}).get("current_module") or 0) != spec.module:
            raise ValueError("Skill 与当前模块不匹配")
        status = "active"
    if status not in spec.allowed_statuses:
        raise ValueError("Skill 不允许在当前状态运行")
    return decision


def _compact_lines(value, limit=7):
    lines = []
    for raw in str(value or "").splitlines():
        text = re.sub(r"^(?:#{1,4}|[-*]|\d+[.)])\s*", "", raw).strip()
        if text and text not in lines:
            lines.append(text[:240])
        if len(lines) >= limit:
            break
    return lines


def _table_cell(value):
    return re.sub(r"\s+", " ", str(value or "")).replace("|", "｜").strip()


def _selected_choice(output):
    snapshot = (output or {}).get("choice_snapshot") or {}
    choices = snapshot.get("choices") or []
    selected_id = str(snapshot.get("selected_choice_id") or "")
    selected = next((deepcopy(item) for item in choices if str(item.get("choice_id") or "") == selected_id), None)
    if len(choices) != 3 or selected is None:
        raise ValueError("模块候选或最终选择不完整")
    return deepcopy(choices), selected_id, selected


def build_report_payload(module, confirmed_outputs):
    module = int(module)
    prefix = "%s-" % module
    outputs = {
        key: deepcopy(item) for key, item in (confirmed_outputs or {}).items()
        if str(key).startswith(prefix) and isinstance(item, dict)
    }
    skill_id = MODULE_SKILL_IDS.get(module)
    if not skill_id:
        raise ValueError("仅模块 1-4 支持报告字段")
    if module in {1, 2, 3}:
        first = outputs.get("%s-1" % module) or {}
        final = outputs.get("%s-2" % module) or {}
        choices, selected_id, selected = _selected_choice(final)
        return {
            "skill_id": skill_id,
            "skill_version": SKILL_REGISTRY[skill_id].contract_version,
            "module": module,
            "module_name": MODULE_NAMES[module],
            "keywords": _compact_lines(first.get("content"), 7),
            "final_conclusion": "%s：%s" % (selected.get("title") or "", selected.get("summary") or ""),
            "candidates": choices,
            "selected_choice_id": selected_id,
            "selected_basis": selected.get("reason") or "本人最终选择",
            "communication_card": {
                "core_expression": selected.get("summary") or selected.get("title") or "",
                "usage": "用于账号简介、置顶内容和核心表达。",
                "boundary": selected.get("caution") or "只使用本人已确认事实。",
            },
        }
    required = ["4-1", "4-2", "4-3", "4-4"]
    if any(not str((outputs.get(key) or {}).get("content") or "").strip() for key in required):
        raise ValueError("模块 4 的四个故事断点不完整")
    sections = [
        {
            "checkpoint": index,
            "title": str(outputs[key].get("title") or ""),
            "content": str(outputs[key].get("content") or "").strip(),
        }
        for index, key in enumerate(required, 1)
    ]
    evidence_quotes = []
    for section in sections:
        evidence_quotes.extend(
            match.strip()
            for match in re.findall(r"(?:事实原话|未来方向原话)[：:]\s*([^\n]+)", section["content"])
            if match.strip() not in evidence_quotes
        )
    return {
        "skill_id": skill_id,
        "skill_version": SKILL_REGISTRY[skill_id].contract_version,
        "module": module,
        "module_name": MODULE_NAMES[module],
        "final_conclusion": sections[-1]["content"],
        "sections": sections,
        "evidence_quotes": evidence_quotes,
    }


def attach_report_payload(state, module):
    key = "%s-%s" % (module, 2 if int(module) in {1, 2, 3} else 4)
    output = (((state or {}).get("ip_profile") or {}).get("confirmed_outputs") or {}).get(key)
    if not isinstance(output, dict):
        raise ValueError("模块最终确认结果不存在")
    output["report_payload"] = build_report_payload(
        module, ((state or {}).get("ip_profile") or {}).get("confirmed_outputs") or {}
    )
    return output["report_payload"]


def _canonical_digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_foundation_snapshot(state):
    profile = (state or {}).get("ip_profile") or {}
    outputs = profile.get("confirmed_outputs") or {}
    modules = []
    for module in range(1, 5):
        key = "%s-%s" % (module, 2 if module < 4 else 4)
        payload = ((outputs.get(key) or {}).get("report_payload"))
        if not isinstance(payload, dict) or int(payload.get("module") or 0) != module:
            raise ValueError("模块 %s 缺少已确认报告字段" % module)
        modules.append(deepcopy(payload))
    facts = []
    for bucket_name in ("facts", "preferences"):
        for field, item in sorted((profile.get(bucket_name) or {}).items()):
            if field in PRIVATE_INTAKE_FIELDS or not isinstance(item, dict):
                continue
            value = str(item.get("value") or "").strip()
            if value:
                facts.append({
                    "field": str(field), "label": FACT_LABELS.get(str(field), str(field)), "value": value,
                    "evidence_quote": str(item.get("evidence_quote") or "").strip(),
                })
    intake = (state or {}).get("intake") or {}
    snapshot = {
        "schema": "ip12.foundation-snapshot/v1",
        "pipeline_version": SKILL_PIPELINE_V1,
        "source_revision": int((state or {}).get("revision") or 1),
        "modules": modules,
        "confirmed_facts": facts,
        "declined_fields": sorted(set(intake.get("declined_fields") or [])),
    }
    snapshot["sha256"] = _canonical_digest(snapshot)
    snapshot["snapshot_id"] = snapshot["sha256"].split(":", 1)[1][:16]
    return snapshot


def validate_foundation_snapshot(snapshot, state):
    if not isinstance(snapshot, dict) or snapshot.get("schema") != "ip12.foundation-snapshot/v1":
        raise ValueError("模块 1-4 冻结快照无效")
    digest_source = {key: deepcopy(value) for key, value in snapshot.items() if key not in {"sha256", "snapshot_id"}}
    digest = _canonical_digest(digest_source)
    if digest != snapshot.get("sha256") or snapshot.get("snapshot_id") != digest.split(":", 1)[1][:16]:
        raise ValueError("模块 1-4 冻结快照摘要不匹配")
    current_state = deepcopy(state)
    current_state["revision"] = int(snapshot.get("source_revision") or 1)
    current = build_foundation_snapshot(current_state)
    if current.get("sha256") != snapshot.get("sha256"):
        raise ValueError("模块结果已经更新，请重新确认最新快照")
    return snapshot


def compile_foundation_markdown(snapshot):
    modules = snapshot.get("modules") or []
    if len(modules) != 4:
        raise ValueError("固定报告必须包含模块 1-4")
    m1, m2, m3, m4 = modules
    lines = [
        "## 首页｜IP结论总览",
        "#### 定位", m1["final_conclusion"],
        "#### 人设", m2["final_conclusion"],
        "#### 价值主张", m3["final_conclusion"],
        "#### 核心故事", _compact_lines(m4["final_conclusion"], 1)[0],
        "#### 下一步", "先用已确认定位、人设、价值主张和核心故事完成首批内容验证。",
    ]
    module_titles = {1: "模块一｜定位诊断", 2: "模块二｜人设塑造", 3: "模块三｜价值主张提炼"}
    card_titles = {1: "传播建议", 2: "人设传播建议", 3: "表达建议"}
    for payload in (m1, m2, m3):
        lines.extend([
            "## " + module_titles[payload["module"]],
            "### 最终结论", payload["final_conclusion"],
            "### 核心关键词", "、".join(payload.get("keywords") or ["待本人确认"]),
            "### 候选方案对照", "| 候选 | 适配点 | 风险 | 状态 |", "|---|---|---|---|",
        ])
        for candidate in payload["candidates"]:
            status = "最终选择" if candidate.get("choice_id") == payload["selected_choice_id"] else "未选择"
            lines.append("| %s｜%s | %s | %s | %s |" % (
                _table_cell(candidate.get("title")), _table_cell(candidate.get("summary")),
                _table_cell(candidate.get("reason")), _table_cell(candidate.get("caution")), status,
            ))
        card = payload["communication_card"]
        lines.extend([
            "### " + card_titles[payload["module"]],
            "#### 核心表达", card["core_expression"],
            "#### 适用场景", card["usage"],
            "#### 表达边界", card["boundary"],
        ])
    lines.extend(["## 模块四｜故事资产挖掘", "### 已确认故事资产"])
    for section in m4.get("sections") or []:
        lines.extend(["#### %s" % section["title"], section["content"]])
    lines.extend([
        "## 内容与执行路径",
        "### P0｜起步", "- 统一账号简介、置顶内容和核心表达。", "- 从一个已确认故事开始发布。",
        "### P1｜持续发布", "- 围绕定位、人设、价值主张和故事资产持续积累内容。", "- 记录真实反馈，不把计划写成结果。",
        "### P2｜阶段验证", "- 按本人确认的平台、投入时间和目标复盘。", "- 未确认统计口径继续标为待本人确认。",
        "## 事实附录与确认清单",
        "### 本人确认资料",
    ])
    lines.extend("- %s：%s" % (item.get("label") or item["field"], item["value"]) for item in snapshot.get("confirmed_facts") or [])
    lines.extend([
        "### 不能夸大的边界",
        "- 未来计划不得写成已经发生的结果。",
        "- 未确认的客户、收入、成交、流量和效果不得补写。",
        "- 传播建议不等于事实或效果承诺。",
        "### 待本人确认项",
    ])
    declined = snapshot.get("declined_fields") or []
    lines.append("- 本人选择跳过：%s" % "、".join(declined) if declined else "- 当前无新增待确认项。")
    return "\n".join(lines).strip() + "\n"
