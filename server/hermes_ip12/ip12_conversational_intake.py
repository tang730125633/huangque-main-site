"""Conversational intake overlay for IP12. Installed onto ip12_harness after import."""

import sys


_HELPERS = 'INTAKE_CORE_FIELDS = (\n    "preferred_name", "current_identity", "core_skill_1", "core_skill_2",\n    "target_audience", "help_goal", "primary_platform", "niche",\n)\nINTAKE_CORE_FIELD_SET = frozenset(INTAKE_CORE_FIELDS)\nCHAT_START_RE = re.compile(\n    r"我先聊聊|先聊聊|不想填表|先进入定位|跳过剩余|其余跳过|先开始定位|先不用填"\n)\nWHOLE_FORM_SKIP_RE = re.compile(r"我先聊聊|不想填表|先进入定位")\n\n\ndef intake_core_gaps(value, updates=()):\n    unknown = set(intake_coverage_gaps(value, updates))\n    return [field for field in INTAKE_CORE_FIELDS if field in unknown]\n\n\ndef wants_chat_start(message):\n    return bool(CHAT_START_RE.search(str(message or "")))\n\n\ndef _wants_whole_form_skip(message):\n    return bool(WHOLE_FORM_SKIP_RE.search(str(message or "")))\n\n\ndef _decline_remaining_intake_fields(state, updates=(), include_core=False):\n    declined = list(state["intake"].get("declined_fields") or [])\n    remaining = intake_coverage_gaps(state, updates)\n    for field in remaining:\n        if field in declined:\n            continue\n        if include_core or field not in INTAKE_CORE_FIELD_SET:\n            declined.append(field)\n    state["intake"]["declined_fields"] = declined\n    return declined\n\n'

_APPLY_INTAKE_DECISION = 'def apply_intake_decision(value, raw, evidence_text, current_message=""):\n    state = normalize_state(value)\n    intake = state["intake"]\n    if intake["status"] == "complete":\n        raise HarnessError("基础资料已经确认")\n    declined = list(intake.get("declined_fields") or [])\n    last_topic = (intake.get("asked_follow_ups") or [""])[-1]\n    for field in _declined_intake_fields(current_message or evidence_text, last_topic):\n        if field not in declined:\n            declined.append(field)\n    intake["declined_fields"] = declined\n    candidate = deepcopy(raw) if isinstance(raw, dict) else raw\n    if isinstance(candidate, dict):\n        candidate_kind = str(candidate.get("decision") or "")\n        if candidate_kind in {"ask_follow_up", "answer_only"}:\n            candidate.update(checkpoint=0, draft="", self_review="")\n            if candidate_kind == "answer_only":\n                candidate["profile_updates"] = []\n        elif candidate_kind == "revise_intake":\n            candidate.update(decision="propose_checkpoint", checkpoint=1)\n    prior_quotes = "\\n".join(\n        str(item.get("evidence_quote") or "")\n        for item in intake.get("profile_updates") or []\n        if isinstance(item, dict)\n    )\n    decision = validate_model_decision(\n        candidate,\n        state,\n        str(evidence_text or "") + "\\n" + prior_quotes,\n        expected_checkpoint=1,\n        allow_partial_profile_updates=True,\n    )\n    for item in (intake.get("profile_updates") or []) + decision["profile_updates"]:\n        if (\n            item.get("field") in INTAKE_FIELD_SET\n            and DECLINE_RE.search(str(item.get("value") or ""))\n            and item["field"] not in declined\n        ):\n            declined.append(item["field"])\n    intake["declined_fields"] = declined\n    merged_updates = {}\n    for item in (intake.get("profile_updates") or []) + decision["profile_updates"]:\n        if item["field"] not in declined:\n            merged_updates[item["field"]] = item\n    merged_update_list = list(merged_updates.values())\n    if decision["decision"] in {"ask_follow_up", "propose_checkpoint"}:\n        decision["profile_updates"] = merged_update_list\n    user_text = current_message or evidence_text\n    chat_start = wants_chat_start(user_text)\n    whole_form = _wants_whole_form_skip(user_text)\n    if chat_start:\n        declined = _decline_remaining_intake_fields(\n            state, merged_update_list, include_core=whole_form\n        )\n    coverage_gaps = intake_coverage_gaps(state, merged_update_list)\n    core_gaps = intake_core_gaps(state, merged_update_list)\n    gap_labels = "、".join(INTAKE_COVERAGE_LABELS[field] for field in core_gaps[:6])\n    if (\n        intake["status"] in {"collecting", "editing"}\n        and decision["decision"] == "answer_only"\n        and core_gaps\n        and not chat_start\n    ):\n        raise HarnessError("核心资料仍有未覆盖项目；回答用户后必须继续只追问一项")\n    if decision["decision"] == "propose_checkpoint":\n        if core_gaps:\n            raise HarnessError(\n                "核心资料仍有未覆盖项目：%s%s；不能生成核对稿，请继续聊或让用户跳过"\n                % (gap_labels, "等" if len(core_gaps) > 6 else "")\n            )\n        declined = _decline_remaining_intake_fields(state, merged_update_list)\n        coverage_gaps = intake_coverage_gaps(state, merged_update_list)\n        intake.update(\n            status="awaiting_confirmation",\n            round=3,\n            draft=decision["draft"],\n            profile_updates=decision["profile_updates"],\n        )\n    elif decision["decision"] == "ask_follow_up":\n        follow_up_topic = intake_follow_up_topic(decision["reply"])\n        canonical_topic = INTAKE_TOPIC_TO_FIELD.get(follow_up_topic, follow_up_topic)\n        asked_follow_ups = intake.setdefault("asked_follow_ups", [])\n        asked_canonical = {\n            INTAKE_TOPIC_TO_FIELD.get(topic, topic) for topic in asked_follow_ups\n        }\n        if canonical_topic in intake.get("declined_fields", []):\n            raise HarnessError("基础访谈追问了用户已经拒答的信息；请改问其他未回答项")\n        if follow_up_topic in asked_follow_ups:\n            if canonical_topic in coverage_gaps:\n                decision["reply"] = intake_natural_question(canonical_topic, clarifying=True)\n            else:\n                raise HarnessError("基础访谈重复追问了已经回答的信息；请改问其他未回答项")\n        if not canonical_topic and core_gaps:\n            remaining = [field for field in core_gaps if field not in asked_canonical]\n            canonical_topic = (remaining or core_gaps)[0]\n        elif not canonical_topic and coverage_gaps:\n            remaining = [field for field in coverage_gaps if field not in asked_canonical]\n            canonical_topic = (remaining or coverage_gaps)[0]\n        if follow_up_topic and follow_up_topic not in asked_follow_ups:\n            asked_follow_ups.append(follow_up_topic)\n        intake["current_question_field"] = canonical_topic\n        if merged_updates:\n            intake["profile_updates"] = list(merged_updates.values())\n        if intake["status"] == "awaiting_confirmation":\n            intake["status"] = "editing"\n    intake["field_statuses"] = intake_field_statuses(state)\n    _bump(state)\n    reply = decision["reply"]\n    if decision["decision"] == "propose_checkpoint":\n        reply = _render_confirmable_reply(\n            reply, decision["draft"],\n            "请确认资料，或者直接补充、纠正；我会说明理解错在哪里并立即重整。",\n        )\n    return state, decision, reply\n\n'


def install(harness):
    if getattr(harness, "_conversational_intake_installed", False):
        return harness
    g = harness.__dict__
    exec(_HELPERS, g)
    exec(_APPLY_INTAKE_DECISION, g)
    try:
        import ip12_conversational_prompts as prompts
    except ImportError:
        from . import ip12_conversational_prompts as prompts
    exec(prompts.INTAKE_SYSTEM_PROMPT_SRC, g)
    exec(prompts.SYSTEM_PROMPT_SRC, g)
    _orig_apply_action = g["apply_action"]

    def apply_action(value, action, expected_revision, *, request_id="", selected_at="", pipeline_version=None,
                     foundation_artifact_validated=False):
        action_type = str((action or {}).get("type") or "") if isinstance(action, dict) else ""
        if action_type == "confirm_intake":
            state = g["normalize_state"](value)
            gaps = g["intake_core_gaps"](state)
            if gaps:
                raise g["HarnessError"](
                    "核心资料尚未齐：%s；请继续聊，或说「我先聊聊」进入定位"
                    % "、".join(g["INTAKE_COVERAGE_LABELS"][field] for field in gaps[:6])
                )
            g["_decline_remaining_intake_fields"](state)
            value = state
        return _orig_apply_action(
            value, action, expected_revision,
            request_id=request_id, selected_at=selected_at,
            pipeline_version=pipeline_version,
            foundation_artifact_validated=foundation_artifact_validated,
        )

    g["apply_action"] = apply_action
    harness._conversational_intake_installed = True
    return harness


def _boot():
    harness = sys.modules.get("ip12_harness") or sys.modules.get("server.hermes_ip12.ip12_harness")
    if harness is None:
        return
    if hasattr(harness, "apply_intake_decision"):
        install(harness)
        return

    def _on_return(frame, event, arg):
        if event != "return" or frame.f_code.co_name != "<module>":
            return
        name = frame.f_globals.get("__name__")
        if name in {"ip12_harness", "server.hermes_ip12.ip12_harness"}:
            sys.setprofile(None)
            install(sys.modules[name])

    sys.setprofile(_on_return)


_boot()
