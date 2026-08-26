"""HTTP and orchestration boundary for the independent AI Creator Agent."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .planner import (
    ALLOWED_PLATFORMS,
    PLATFORM_LABELS,
    CreatorPlanner,
    PlannerError,
    clear_preferences,
    remember_preference,
    sanitize_platforms,
    sanitize_preferences,
)
from .profile_agent import (
    DeepSeekProfileAgent, MODULES, ProfileAgentError,
    current_question, initial_state,
)
from .store import (
    CreatorAgentStore, IdempotencyConflict, QuoteExpired,
    StateConflict, StoreError, STALE_CLAIM_SECONDS,
)


_PROJECT_RE = re.compile(r"^[0-9a-f]{12}$")
_BATCH_RE = re.compile(r"^creator_batch_[0-9a-f]{32}$")
_REQUEST_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_PRIVATE_KEYS = {"quote_token", "job_id", "idempotency_key", "confirmation_id"}
_RUNNING = {"submitted", "queued", "running", "verifying", "processing", "submission_unknown"}
_FAILED = {"error", "failed", "refunded", "failed_submission"}
_DONE = {"done", "completed", "success"}
_QUOTE_SUBMIT_BASE_MARGIN_SECONDS = 15
_QUOTE_SUBMIT_PER_JOB_SECONDS = 35


class APIError(RuntimeError):
    def __init__(self, status, detail, code="creator_agent_error"):
        super().__init__(detail)
        self.status = int(status)
        self.detail = str(detail)
        self.code = str(code)


def _public(value):
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items() if key not in _PRIVATE_KEYS}
    if isinstance(value, list):
        return [_public(item) for item in value]
    return value


def _url(base, path):
    return str(base or "").rstrip("/") + "/" + str(path or "").lstrip("/")


def _valid_loopback_base(value):
    parsed = urllib.parse.urlsplit(str(value or ""))
    return bool(
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and not parsed.username and not parsed.password
        and not parsed.query and not parsed.fragment
    )


def _auth_headers(headers):
    result = {}
    for name in ("Authorization", "Cookie"):
        value = headers.get(name) if headers else ""
        if value:
            result[name] = value
    return result


class AuthClient:
    def __init__(self, base_url, timeout=10, opener=None):
        self.base_url = str(base_url or "http://127.0.0.1:8095").rstrip("/")
        self.timeout = int(timeout)
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def verify(self, headers):
        request = urllib.request.Request(
            _url(self.base_url, "/api/auth/me"), headers=_auth_headers(headers),
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            raise APIError(401 if exc.code in (401, 403) else 502, "登录状态无效", "unauthorized") from exc
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise APIError(503, "账号服务暂不可用", "auth_unavailable") from exc
        user = result.get("user") if isinstance(result, dict) else None
        if not isinstance(user, dict) or not user.get("username") or not user.get("account_id"):
            raise APIError(401, "请先登录", "unauthorized")
        return user


class BridgeClient:
    def __init__(self, base_url, internal_token, timeout=30, opener=None):
        self.base_url = str(base_url or "http://127.0.0.1:8095").rstrip("/")
        self.internal_token = str(internal_token or "")
        self.timeout = int(timeout)
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _post(self, path, body, timeout=None):
        if not self.internal_token:
            raise APIError(503, "AI 创作助手执行桥未配置", "bridge_not_configured")
        request = urllib.request.Request(
            _url(self.base_url, path),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-HQ-Internal-Token": self.internal_token,
            },
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=timeout or self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read() or b"{}")
            except (ValueError, TypeError):
                data = {}
            raise APIError(
                exc.code, data.get("detail") or "站内能力调用失败",
                data.get("code") or "bridge_error",
            ) from exc
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise APIError(503, "站内执行桥暂不可用", "bridge_unavailable") from exc

    def catalog(self, account_id):
        return self._post("/api/auth/internal/creator-agent/catalog", {"account_id": account_id})

    def health(self):
        return self._post("/api/auth/internal/creator-agent/health", {}, timeout=3)

    def action(self, account_id, action, tool_input, *, confirm=False,
               quote_token="", idempotency_key=""):
        body = {
            "account_id": account_id,
            "action": action,
            "input": tool_input,
            "confirm": bool(confirm),
            "quote_token": quote_token,
        }
        if idempotency_key:
            body["idempotency_key"] = idempotency_key
        return self._post("/api/auth/internal/creator-agent/action", body)

    def reconcile(self, account_id, tool_input, idempotency_key):
        return self._post("/api/auth/internal/creator-agent/reconcile", {
            "account_id": account_id,
            "input": tool_input,
            "idempotency_key": idempotency_key,
        }, timeout=10)


class CreatorAgentService:
    def __init__(self, store, planner, auth, bridge, profile_agent, clock=None):
        self.store = store
        self.planner = planner
        self.auth = auth
        self.bridge = bridge
        self.profile_agent = profile_agent
        self.clock = clock or time.time
        self._locks = {}
        self._locks_guard = threading.Lock()
        self._request_context = threading.local()

    def _lock(self, username, project_id):
        key = username + ":" + project_id
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    def health(self):
        checks = {
            "bridge_token": bool(getattr(self.bridge, "internal_token", "")),
            "auth_url": _valid_loopback_base(getattr(self.auth, "base_url", "")),
            "model_configured": bool(getattr(self.profile_agent, "configured", False)),
            "model_reachable": False,
            "database_writable": bool(self.store.health()),
            "bridge_catalog": False,
        }
        bridge_detail = ""
        if checks["model_configured"]:
            try:
                checks["model_reachable"] = self.profile_agent.health() is True
            except Exception:
                checks["model_reachable"] = False
        if checks["bridge_token"] and checks["auth_url"]:
            try:
                bridge = self.bridge.health()
                actions = set(bridge.get("actions") or []) if isinstance(bridge, dict) else set()
                checks["bridge_catalog"] = bool(
                    bridge.get("ready") is True
                    and {
                        "matrix-template-templates", "matrix-template-generate",
                        "matrix-template-reconcile",
                    }.issubset(actions)
                )
            except APIError as exc:
                bridge_detail = exc.code
        ready = all(checks.values())
        return {
            "ok": True,
            "ready": ready,
            "service": "huangque-creator-agent",
            "version": 3,
            "checks": checks,
            "details": {
                key: value for key, value in {
                    "bridge_catalog": bridge_detail,
                }.items() if value
            },
        }

    def capability(self, user):
        catalog = self.bridge.catalog(user["account_id"])
        action = next((
            item for item in catalog.get("actions") or []
            if item.get("action") == "matrix-template-generate"
        ), None)
        template_available = bool(action) and (action.get("availability") or {}).get("status") == "available"
        return {
            "enabled": True, "available": True,
            "template_video_available": template_available,
            "feature": "creator_agent_v1",
        }

    def _gate(self, user):
        catalog = self.bridge.catalog(user["account_id"])
        actions = {
            item.get("action"): item for item in catalog.get("actions") or []
            if isinstance(item, dict)
        }
        return actions

    @staticmethod
    def _project_id(value):
        project_id = str(value or "")
        if not _PROJECT_RE.fullmatch(project_id):
            raise APIError(400, "画像项目编号无效", "invalid_project")
        return project_id

    def _ensure_project(self, user, headers):
        workspaces = self.store.workspaces(user["username"])
        if not workspaces:
            project_id = uuid.uuid4().hex[:12]
            workspace = self.store.ensure_workspace(
                user["username"], project_id, "我的个人画像",
            )
            workspace = self.store.update_workspace(
                user["username"], project_id,
                profile_state=initial_state(), flow={"mode": "profile_interview"},
            )
            question = current_question(workspace["profile_state"])
            self.store.add_message(
                user["username"], project_id, "assistant",
                "我们先建立你的个人画像。" + question["question"],
                public=self._question_public(workspace["profile_state"]),
            )
            workspaces = [workspace]
        owned = {item["project_id"]: item for item in workspaces}
        selected = self.store.active_project(user["username"])
        if selected not in owned:
            selected = next(iter(owned))
            self.store.set_active_project(user["username"], selected)
        workspace = owned[selected]
        if not workspace.get("profile_state"):
            workspace = self.store.update_workspace(
                user["username"], selected,
                profile_state=initial_state(), flow={"mode": "profile_interview"},
            )
            question = current_question(workspace["profile_state"])
            self.store.add_message(
                user["username"], selected, "assistant",
                "独立画像流程已启用。" + question["question"],
                public=self._question_public(workspace["profile_state"]),
            )
        project = self._local_project(workspace)
        return workspaces, project, workspace

    @staticmethod
    def _state(project):
        return project.get("profile_state") if isinstance(project.get("profile_state"), dict) else {}

    @classmethod
    def _foundation_ready(cls, project):
        state = cls._state(project)
        return state.get("profile_ready") is True

    @classmethod
    def _progress(cls, project):
        state = cls._state(project)
        completed = sorted({
            int(item) for item in state.get("completed_modules") or []
            if str(item).isdigit() and 1 <= int(item) <= 6
        })
        ready = cls._foundation_ready(project)
        return {
            "current_module": int(state.get("current_module") or 1),
            "module_step": int(state.get("module_step") or 0),
            "completed_modules": completed,
            "foundation_status": "confirmed" if ready else str(state.get("phase") or "collecting"),
            "foundation_report_id": "",
            "foundation_ready": ready,
            "profile_complete": ready,
        }

    @classmethod
    def _profile_context(cls, project, workspace=None):
        state = cls._state(project)
        profile = project.get("profile") if isinstance(project.get("profile"), dict) else {}
        return {
            "profile": _public(profile),
            "answers": _public(state.get("answers") or {}),
            "selected_modules": _public(state.get("selected_profiles") or {}),
            "completed_modules": cls._progress(project)["completed_modules"],
            "profile_confirmed": cls._foundation_ready(project),
            "creator_profile_overrides": _public((workspace or {}).get("profile_overrides") or {}),
        }

    @staticmethod
    def _local_project(workspace):
        return {
            "id": workspace["project_id"],
            "title": workspace.get("alias") or "我的个人画像",
            "updated": str(workspace.get("updated_at") or ""),
            "profile_state": workspace.get("profile_state") or {},
            "profile": workspace.get("profile") or {},
            "reports": (workspace.get("profile_state") or {}).get("module_reviews") or {},
            "deliverables": workspace.get("deliverables") or {},
            "artifacts": [],
        }

    @classmethod
    def _public_project(cls, project, workspace):
        progress = cls._progress(project)
        return {
            "id": project.get("id"),
            "title": project.get("title") or "我的个人画像",
            "display_name": workspace.get("alias") or project.get("title") or "我的个人画像",
            "updated": project.get("updated") or "",
            "revision": int(cls._state(project).get("revision") or 1),
            "progress": progress,
            "harness_actions": [],
            "reports": _public(project.get("reports") or {}),
            "deliverables": _public(project.get("deliverables") or {}),
            "artifacts": _public(project.get("artifacts") or []),
            "profile": _public(project.get("profile") or {}),
            "foundation_pdf_url": "",
        }

    @staticmethod
    def _question_public(state):
        question = current_question(state)
        actions = [
            {"intent": "profile_answer", "label": option, "payload": {
                "answer": option, "profile_revision": int(state.get("revision") or 1),
                "module": question["module"], "field": question["key"],
            }}
            for option in question.get("options") or []
        ]
        return {
            "kind": "profile_question", "module": question["module"],
            "module_name": question["module_name"], "field": question["key"],
            "template": question.get("template") or "", "actions": actions,
        }

    def _project_list(self, username, projects, active_id, workspace):
        result = []
        for item in projects:
            if not isinstance(item, dict) or not item.get("project_id"):
                continue
            item_workspace = (
                workspace if item["project_id"] == active_id else item
            ) or {}
            result.append({
                "id": item["project_id"],
                "title": item_workspace.get("alias") or "我的个人画像",
                "updated": str(item.get("updated_at") or ""),
                "active": item["project_id"] == active_id,
            })
        return result

    def _templates(self, user):
        actions = self._gate(user)
        matrix = actions.get("matrix-template-generate")
        if not matrix or (matrix.get("availability") or {}).get("status") != "available":
            raise APIError(503, "模板视频维护中，画像与文案功能仍可使用", "template_unavailable")
        value = self.bridge.action(user["account_id"], "matrix-template-templates", {})
        templates = value.get("templates") if isinstance(value, dict) else []
        if not isinstance(templates, list) or not templates:
            raise APIError(503, "模板目录暂不可用", "template_catalog_unavailable")
        return templates

    def _snapshot(self, user, headers, projects, project, workspace):
        batches = self.store.batches(user["username"], project["id"])
        public_project = self._public_project(project, workspace)
        quick_actions = []
        if public_project["progress"]["foundation_ready"]:
            quick_actions = [
                {"intent": "start_video", "label": "开始制作视频"},
                {"intent": "topic_plan", "label": "生成选题计划"},
                {"intent": "modify_profile", "label": "修改我的画像"},
            ]
        return {
            "user": _public(user),
            "projects": self._project_list(user["username"], projects, project["id"], workspace),
            "project": public_project,
            "workspace": _public(workspace),
            "messages": self.store.messages(user["username"], project["id"]),
            "quick_actions": quick_actions,
            "platforms": [
                {"id": key, "label": PLATFORM_LABELS[key]} for key in ALLOWED_PLATFORMS
            ],
            "material_packs": [
                {"id": "platform-default-" + key, "platform": key,
                 "name": PLATFORM_LABELS[key] + "平台素材库"}
                for key in ALLOWED_PLATFORMS
            ],
            "batches": batches,
            "latest_batch": batches[0] if batches else None,
        }

    def bootstrap(self, user, headers):
        self._gate(user)
        projects, project, workspace = self._ensure_project(user, headers)
        return self._snapshot(user, headers, projects, project, workspace)

    def select_project(self, user, headers, project_id):
        self._gate(user)
        project_id = self._project_id(project_id)
        projects = self.store.workspaces(user["username"])
        if project_id not in {item["project_id"] for item in projects}:
            raise APIError(404, "画像项目不存在", "not_found")
        self.store.set_active_project(user["username"], project_id)
        workspace = self.store.workspace(user["username"], project_id)
        project = self._local_project(workspace)
        return self._snapshot(user, headers, projects, project, workspace)

    def rename_project(self, user, headers, project_id, body):
        self._gate(user)
        project_id = self._project_id(project_id)
        workspace = self.store.workspace(user["username"], project_id)
        if not workspace:
            raise APIError(404, "画像项目不存在", "not_found")
        alias = re.sub(r"\s+", " ", str((body or {}).get("title") or "")).strip()[:120]
        if not alias:
            raise APIError(400, "画像名称不能为空", "invalid_title")
        return self.store.update_workspace(user["username"], project_id, alias=alias)

    def _record_assistant(self, user, project_id, content, public=None):
        self.store.add_message(
            user["username"], project_id, "assistant", content, public=public or {},
        )

    def _response(self, user, headers, project_id, reply, public=None):
        projects = self.store.workspaces(user["username"])
        workspace = self.store.workspace(user["username"], project_id)
        project = self._local_project(workspace)
        snapshot = self._snapshot(user, headers, projects, project, workspace)
        return {"reply": reply, "message_public": _public(public or {}), **snapshot}

    @staticmethod
    def _review_public(review, state):
        return {
            "kind": "profile_review",
            "module": review["module"],
            "module_name": review["module_name"],
            "summary": review["summary"],
            "options": review["options"],
            "actions": [
                {
                    "intent": "profile_choice",
                    "label": "选择：" + str(item.get("title") or "方案 %d" % (index + 1))[:40],
                    "payload": {
                        "choice_index": index,
                        "profile_revision": int(state.get("revision") or 1),
                        "module": review["module"],
                    },
                }
                for index, item in enumerate(review["options"])
            ],
        }

    def _profile_model_error(self, error):
        self._discard_current_message()
        raise APIError(503, "DeepSeek V4 Flash 暂不可用，请稍后重试", "creator_model_unavailable") from error

    def _discard_current_message(self):
        username = getattr(self._request_context, "username", "")
        message_id = getattr(self._request_context, "message_id", 0)
        if username and message_id:
            self.store.delete_message_if_unanswered(username, message_id)
        self._request_context.username = ""
        self._request_context.message_id = 0

    def _save_profile_state(self, user, workspace, state, expected_revision,
                            *, profile=None, deliverables=None, flow=None):
        try:
            return self.store.update_profile_state(
                user["username"], workspace["project_id"], state,
                expected_revision, profile=profile,
                deliverables=deliverables, flow=flow,
            )
        except StateConflict as exc:
            self._discard_current_message()
            raise APIError(
                409, "画像已在其他页面更新，请刷新后继续",
                "profile_state_conflict",
            ) from exc

    def _profile_turn(self, user, workspace, message, intent, payload):
        state = dict(workspace.get("profile_state") or initial_state())
        expected_revision = int(state.get("revision") or 1)
        module = max(1, min(4, int(state.get("current_module") or 1)))
        if intent in {"profile_answer", "profile_choice"}:
            try:
                expected_revision = int(payload.get("profile_revision"))
            except (TypeError, ValueError) as exc:
                self._discard_current_message()
                raise APIError(400, "画像版本缺失，请刷新后重试", "profile_revision_required") from exc
            if expected_revision != int(state.get("revision") or 1):
                self._discard_current_message()
                raise APIError(409, "画像步骤已经变化，请使用最新问题", "profile_state_conflict")
            if state.get("profile_ready"):
                self._discard_current_message()
                raise APIError(409, "画像已经完成，旧选项不能再次提交", "profile_state_conflict")
        if state.get("phase") == "review":
            review = (state.get("module_reviews") or {}).get(str(module)) or {}
            choice = payload.get("choice_index") if intent == "profile_choice" else None
            if choice is None and re.search(r"(?:确认|选择|采用|就要|第)[一二三123]?", message):
                matched = re.search(r"[一二三123]", message)
                if matched:
                    token = matched.group(0)
                    choice = {"一": 0, "二": 1, "三": 2}.get(
                        token, int(token) - 1 if token.isdigit() else 0,
                    )
                else:
                    choice = 0
            if choice is not None:
                try:
                    choice = int(choice)
                    selected = review["options"][choice]
                except (TypeError, ValueError, IndexError, KeyError) as exc:
                    raise APIError(400, "请选择有效的画像方案", "invalid_profile_choice") from exc
                selected_profiles = dict(state.get("selected_profiles") or {})
                selected_profiles[str(module)] = selected
                completed = sorted(set(state.get("completed_modules") or []) | {module})
                state.update({
                    "selected_profiles": selected_profiles,
                    "completed_modules": completed,
                    "revision": int(state.get("revision") or 1) + 1,
                })
                if module < 4:
                    state.update({
                        "current_module": module + 1, "question_index": 0,
                        "phase": "collecting",
                    })
                    workspace = self._save_profile_state(
                        user, workspace, state, expected_revision,
                        flow={"mode": "profile_interview"},
                    )
                    question = current_question(state)
                    return workspace, (
                        "已确认%s。接下来进入%s。%s" % (
                            MODULES[module]["name"], MODULES[module + 1]["name"],
                            question["question"],
                        )
                    ), self._question_public(state)
                state.update({"profile_ready": True, "phase": "ready"})
                profile = {
                    "answers": state.get("answers") or {},
                    "modules": selected_profiles,
                    "revision": state["revision"],
                }
                deliverables = dict(workspace.get("deliverables") or {})
                deliverables["personal_profile"] = {
                    "title": "个人画像",
                    "content": profile,
                }
                workspace = self._save_profile_state(
                    user, workspace, state, expected_revision,
                    profile=profile, deliverables=deliverables, flow={"mode": "idle"},
                )
                return workspace, (
                    "个人画像已完成并保存。现在可以生成选题计划，或直接制作模板视频。"
                ), {"kind": "profile_completed", "profile": _public(profile)}
            try:
                revised = self.profile_agent.revise_module_review(state, module, message)
            except ProfileAgentError as exc:
                self._profile_model_error(exc)
            reviews = dict(state.get("module_reviews") or {})
            reviews[str(module)] = revised
            state["module_reviews"] = reviews
            state["revision"] = int(state.get("revision") or 1) + 1
            workspace = self._save_profile_state(
                user, workspace, state, expected_revision,
            )
            return workspace, "已按你的要求更新本模块，请重新选择。", self._review_public(revised, state)

        answer = str(payload.get("answer") or message).strip()
        try:
            captured = self.profile_agent.capture_answer(state, answer)
        except ProfileAgentError as exc:
            self._profile_model_error(exc)
        if not captured["accepted"]:
            return workspace, captured["clarification"], self._question_public(state)
        question = current_question(state)
        answers = dict(state.get("answers") or {})
        module_answers = dict(answers.get(str(module)) or {})
        module_answers[question["key"]] = captured["value"]
        answers[str(module)] = module_answers
        state["answers"] = answers
        next_index = int(state.get("question_index") or 0) + 1
        questions = MODULES[module]["questions"]
        state["revision"] = int(state.get("revision") or 1) + 1
        if next_index < len(questions):
            state["question_index"] = next_index
            workspace = self._save_profile_state(
                user, workspace, state, expected_revision,
            )
            next_question = current_question(state)
            return workspace, captured["ack"] + "\n\n" + next_question["question"], self._question_public(state)
        try:
            review = self.profile_agent.build_module_review(state, module)
        except ProfileAgentError as exc:
            self._profile_model_error(exc)
        reviews = dict(state.get("module_reviews") or {})
        reviews[str(module)] = review
        state.update({"module_reviews": reviews, "phase": "review"})
        workspace = self._save_profile_state(
            user, workspace, state, expected_revision,
        )
        return workspace, captured["ack"] + "\n\n" + review["summary"], self._review_public(review, state)

    def _generate_topic_plan(self, user, workspace, platforms, request):
        profile = workspace.get("profile") or {}
        try:
            result = self.profile_agent.topic_plan(profile, platforms, request)
        except ProfileAgentError as exc:
            self._profile_model_error(exc)
        deliverables = dict(workspace.get("deliverables") or {})
        key = "topic_plan_%d" % (len([item for item in deliverables if item.startswith("topic_plan_")]) + 1)
        deliverables[key] = {
            "title": "选题与文案计划",
            "content": result,
        }
        workspace = self.store.update_workspace(
            user["username"], workspace["project_id"],
            deliverables=deliverables, platforms=platforms, flow={"mode": "idle"},
        )
        return workspace, str(result.get("reply") or "选题与文案已经生成并保存。"), {
            "kind": "topic_plan", "result": _public(result),
        }

    @staticmethod
    def _intent_from_message(message):
        text = re.sub(r"\s+", "", str(message or ""))
        if re.fullmatch(r"(?:确认|确定|采用|就按)(?:当前)?方案", text):
            return "confirm_plan"
        if re.search(r"(?:查看|显示).*(?:偏好|习惯)", text):
            return "view_preferences"
        if re.search(r"(?:清空|删除|重置).*(?:偏好|习惯)", text):
            return "clear_preferences"
        if re.search(r"(?:修改|改成|删掉|删除|重写).*(?:第[一二三123]篇|文案|口播稿)|(?:第[一二三123]篇|文案|口播稿).*(?:修改|改成|删掉|删除|重写)", text):
            return "revise_copy"
        if re.search(r"(?:选题计划|选题库|生成选题|写文案|口播文案)", text):
            return "topic_plan"
        if (
            re.match(r"^(?:我想|请)?(?:(?:再|重新)(?:生成|做)(?:一条|一个|一版)?(?:视频)?|重做(?:视频)?)", text)
            or re.search(r"修改.*(?:标题|底部|行动文案|视频)|(?:标题|底部|行动文案).*(?:改成|换成|修改)", text)
        ):
            return "regenerate_video"
        if re.search(r"(?:制作|生成|做).*(?:模板)?视频|模板成片", text):
            return "start_video"
        if re.search(r"修改.*画像|调整.*画像|更新.*画像", text):
            return "modify_profile"
        return ""

    @staticmethod
    def _message_request_hash(project_id, message, intent, payload):
        return hashlib.sha256(json.dumps({
            "project_id": str(project_id or ""),
            "message": str(message or ""),
            "intent": str(intent or ""),
            "payload": payload if isinstance(payload, dict) else {},
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    @staticmethod
    def _value_hash(value):
        return hashlib.sha256(json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    @staticmethod
    def _platforms_from_text(message):
        text = str(message or "")
        return [key for key in ALLOWED_PLATFORMS if PLATFORM_LABELS[key] in text]

    @staticmethod
    def _topic_from_text(message):
        text = re.sub(r"\s+", " ", str(message or "")).strip()
        text = re.sub(
            r"^(?:请|帮我|我要|我想)?(?:开始)?(?:制作|生成|做)[^，,:：]{0,30}?视频",
            "", text,
        )
        for label in PLATFORM_LABELS.values():
            text = text.replace(label, " ")
        text = re.sub(r"^(?:平台|发布到|发到|和|、|，|,|:|：)+", "", text).strip()
        text = re.sub(r"^(?:主题|选题|核心观点)(?:是|为)?[：: ]*", "", text).strip()
        return text[:400]

    def _preferences_reply(self, workspace):
        value = sanitize_preferences(workspace.get("template_video_preferences"))
        lines = []
        if value["global"]:
            lines.append("全局：" + "；".join(value["global"]))
        for key in ALLOWED_PLATFORMS:
            if value["platforms"].get(key):
                lines.append(PLATFORM_LABELS[key] + "：" + "；".join(value["platforms"][key]))
        return "当前没有已记录的模板视频偏好。" if not lines else "当前偏好\n" + "\n".join(lines)

    def _start_video(self, user, project, workspace, message, payload):
        explicit_platforms = sanitize_platforms((payload or {}).get("platforms"))
        if not explicit_platforms:
            explicit_platforms = self._platforms_from_text(message)
        platforms = explicit_platforms or sanitize_platforms(workspace.get("platforms"))
        topic = re.sub(r"\s+", " ", str((payload or {}).get("topic") or "")).strip()[:400]
        if not topic and message:
            topic = self._topic_from_text(message)
        flow = {
            "mode": "template_collect" if explicit_platforms else "template_platforms",
            "topic": topic, "platforms": platforms,
        }
        self.store.update_workspace(user["username"], project["id"], flow=flow)
        if not explicit_platforms:
            return "这次要发布到哪些平台？可以调整默认选择并多选。", {
                "kind": "platform_picker",
                "platforms": [{"id": key, "label": PLATFORM_LABELS[key]} for key in ALLOWED_PLATFORMS],
                "selected": platforms,
            }
        if not topic:
            return "请告诉我这次视频想讲的主题或核心观点。", {"kind": "topic_prompt"}
        return self._create_video_plan(user, project, workspace, topic, platforms)

    def _create_video_plan(self, user, project, workspace, topic, platforms):
        templates = self._templates(user)
        try:
            planned = self.planner.video_plan(
                self._profile_context(project, workspace), topic, platforms, templates,
                workspace.get("template_video_preferences"),
            )
        except PlannerError as exc:
            self._profile_model_error(exc)
        batch = self.store.create_batch(
            user["username"], project["id"], topic, planned.get("goal") or "",
            planned["platform_plans"],
        )
        self.store.update_workspace(
            user["username"], project["id"], platforms=platforms,
            flow={"mode": "template_review", "batch_id": batch["id"]},
        )
        return "我已按平台分别整理方案。你可以直接说要改哪一项，确认无误后再统一报价。", {
            "kind": "video_plan",
            "batch": _public(batch),
            "actions": [
                {"intent": "adjust_video_platforms", "label": "调整平台"},
                {"intent": "confirm_plan", "label": "确认方案并查看价格", "primary": True},
            ],
        }

    def _revise_video_plan(self, user, workspace, batch, instruction):
        templates = self._templates(user)
        try:
            revised = self.planner.revise_video_plan(
                batch.get("plans") or [], instruction, templates,
                workspace.get("template_video_preferences"),
            )
        except PlannerError as exc:
            self._profile_model_error(exc)
        updated = self.store.replace_batch_plans(
            user["username"], batch["id"], revised["platform_plans"],
            batch["revision"],
        )
        return "已按你的要求更新方案，请继续修改或确认方案。", {
            "kind": "video_plan", "batch": _public(updated),
            "actions": [
                {"intent": "adjust_video_platforms", "label": "调整平台"},
                {"intent": "confirm_plan", "label": "确认方案并查看价格", "primary": True},
            ],
        }

    def _new_video_version(self, user, project, workspace, batch, instruction):
        templates = self._templates(user)
        mentioned = self._platforms_from_text(instruction)
        source_plans = [
            item for item in batch.get("plans") or []
            if not mentioned or item.get("platform") in mentioned
        ]
        try:
            revised = self.planner.revise_video_plan(
                source_plans, instruction, templates,
                workspace.get("template_video_preferences"),
            )
        except PlannerError as exc:
            self._profile_model_error(exc)
        created = self.store.create_batch(
            user["username"], project["id"], batch.get("topic") or "新版本",
            revised.get("goal") or batch.get("goal") or "", revised["platform_plans"],
        )
        self.store.update_workspace(
            user["username"], project["id"],
            flow={"mode": "template_review", "batch_id": created["id"]},
        )
        return "已基于上一版创建新方案。确认后会重新报价，原视频和原任务保持不变。", {
            "kind": "video_plan", "batch": _public(created),
            "actions": [
                {"intent": "adjust_video_platforms", "label": "调整平台"},
                {"intent": "confirm_plan", "label": "确认方案并查看价格", "primary": True},
            ],
        }

    @staticmethod
    def _expected_revision(value):
        if isinstance(value, bool):
            raise APIError(400, "方案版本无效", "invalid_revision")
        try:
            revision = int(value)
        except (TypeError, ValueError) as exc:
            raise APIError(400, "请刷新后重试", "revision_required") from exc
        if revision < 1:
            raise APIError(400, "方案版本无效", "invalid_revision")
        return revision

    @staticmethod
    def _expected_quote_expiry(value):
        if isinstance(value, bool):
            raise APIError(400, "报价版本无效", "invalid_quote_expiry")
        try:
            expires_at = int(value)
        except (TypeError, ValueError) as exc:
            raise APIError(400, "请重新获取报价", "quote_expiry_required") from exc
        if expires_at <= 0:
            raise APIError(400, "报价版本无效", "invalid_quote_expiry")
        return expires_at

    @staticmethod
    def _raise_store_conflict(error):
        if isinstance(error, QuoteExpired):
            raise APIError(
                409, "报价已过期或剩余时间不足，请重新报价",
                "quote_expired",
            ) from error
        code = "idempotency_conflict" if isinstance(error, IdempotencyConflict) else "state_conflict"
        raise APIError(409, "方案状态已经变化，请刷新后重试", code) from error

    @staticmethod
    def _quote_safety_margin(job_count):
        return (
            STALE_CLAIM_SECONDS
            + _QUOTE_SUBMIT_BASE_MARGIN_SECONDS
            + max(1, int(job_count)) * _QUOTE_SUBMIT_PER_JOB_SECONDS
        )

    def quote_batch(self, user, batch_id, expected_revision):
        batch_id = str(batch_id or "")
        if not _BATCH_RE.fullmatch(batch_id):
            raise APIError(400, "视频方案编号无效", "invalid_batch")
        revision = self._expected_revision(expected_revision)
        current = self.store.batch(user["username"], batch_id, include_private=True)
        if not current:
            raise APIError(404, "视频方案不存在", "not_found")
        if current["revision"] != revision:
            raise APIError(409, "方案版本已经变化，请刷新后重试", "state_conflict")
        now = int(self.clock())
        safety_margin = self._quote_safety_margin(len(current.get("jobs") or []))
        try:
            batch = self.store.claim_quote(
                user["username"], batch_id, revision,
                now=now, minimum_validity=safety_margin,
            )
        except (StateConflict, StoreError) as exc:
            self._raise_store_conflict(exc)
        if batch.get("quote_reused"):
            return self.store.batch(user["username"], batch_id)
        claim_id = batch["claim_id"]
        items, total, points, expirations, job_quotes = [], 0, None, [], []
        try:
            for job in batch["jobs"]:
                result = self.bridge.action(
                    user["account_id"], "matrix-template-generate", job["input"], confirm=False,
                )
                token = str(result.get("quote_token") or "")
                if not token:
                    raise APIError(502, "模板视频没有返回有效报价", "quote_invalid")
                public_quote = _public(result)
                try:
                    cost = int(public_quote.get("cost") or 0)
                except (TypeError, ValueError):
                    cost = 0
                if cost <= 0:
                    raise APIError(502, "模板视频报价无效", "quote_invalid")
                total += cost
                try:
                    current_points = int(public_quote.get("points"))
                except (TypeError, ValueError):
                    current_points = None
                quote_now = int(self.clock())
                try:
                    expires_at = int(public_quote.get("expires_at"))
                except (TypeError, ValueError):
                    expires_at = 0
                if expires_at <= 0:
                    try:
                        expires_at = quote_now + int(public_quote.get("expires_in"))
                    except (TypeError, ValueError):
                        expires_at = 0
                if expires_at <= quote_now:
                    raise APIError(502, "模板视频报价已失效", "quote_invalid")
                public_quote["expires_at"] = expires_at
                public_quote["expires_in"] = max(0, expires_at - quote_now)
                if current_points is not None:
                    points = current_points if points is None else min(points, current_points)
                expirations.append(expires_at)
                items.append({
                    "platform": job["platform"], "label": PLATFORM_LABELS[job["platform"]],
                    "cost": cost,
                })
                job_quotes.append({
                    "id": job["id"], "input_hash": job["input_hash"],
                    "quote_token": token, "quote": public_quote,
                    "cost": cost, "expires_at": expires_at,
                })
            finished_at = int(self.clock())
            earliest_expiry = min(expirations)
            if earliest_expiry <= finished_at + safety_margin:
                raise APIError(
                    409, "报价剩余时间不足，请重新报价",
                    "quote_expired",
                )
            quote = {
                "items": items, "total_cost": total, "points": points,
                "expires_at": earliest_expiry,
                "expires_in": max(0, earliest_expiry - finished_at),
                "confirmation_required": True,
            }
            return self.store.finish_quote(
                user["username"], batch_id, claim_id, job_quotes, quote,
                now=finished_at,
            )
        except Exception:
            self.store.abort_quote(user["username"], batch_id, claim_id)
            raise

    @staticmethod
    def _submission_uncertain(error):
        return error.status >= 500 or error.code in {
            "bridge_unavailable", "upstream_unavailable", "idempotency_in_progress",
            "result_unknown", "submit_result_unknown", "cli_internal_error",
        }

    @staticmethod
    def _submission_result(result):
        provider_job = str(result.get("job_id") or "")
        status = str(result.get("status") or ("running" if provider_job else "submitted"))
        if not provider_job and status not in _DONE and status not in _FAILED:
            raise APIError(502, "任务提交结果待确认", "submit_result_unknown")
        if status in _DONE:
            status = "done"
        elif status in _FAILED:
            status = "failed"
        elif provider_job:
            status = "running"
        return {
            "status": status, "job_id": provider_job,
            "result": _public(result), "error": str(result.get("error") or "")[:500],
            "refund_status": str(result.get("refund_status") or ""),
        }

    def _submit_matrix_job(self, user, job):
        tool_input = job.get("submit_input") or {}
        input_hash = str(job.get("submit_input_hash") or "")
        quote_token = str(job.get("submit_quote_token") or "")
        quote_cost = int(job.get("submit_quote_cost") or 0)
        quote_expires_at = int(job.get("submit_quote_expires_at") or 0)
        idempotency_key = str(job.get("submit_idempotency_key") or "")
        if (
            not tool_input or not input_hash
            or input_hash != self._value_hash(tool_input)
            or not quote_token or quote_cost <= 0 or quote_expires_at <= 0
            or not idempotency_key
        ):
            raise APIError(409, "冻结提交快照不完整", "submit_snapshot_invalid")
        result = self.bridge.action(
            user["account_id"], "matrix-template-generate", tool_input,
            confirm=True, quote_token=quote_token,
            idempotency_key=idempotency_key,
        )
        return self._submission_result(result)

    def _recover_matrix_job(self, user, job):
        tool_input = job.get("submit_input") or {}
        idempotency_key = str(job.get("submit_idempotency_key") or "")
        if not tool_input or not idempotency_key:
            raise APIError(409, "冻结恢复快照不完整", "submit_snapshot_invalid")
        try:
            result = self.bridge.reconcile(
                user["account_id"], tool_input, idempotency_key,
            )
        except APIError as exc:
            if exc.code != "idempotency_not_found":
                raise
            if int(job.get("submit_quote_expires_at") or 0) <= int(self.clock()):
                raise APIError(
                    409,
                    "原提交未被内容服务受理，且报价已经过期，请重新报价后生成",
                    "submission_not_accepted",
                ) from exc
            return self._submit_matrix_job(user, job)
        return self._submission_result(result)

    def confirm_batch(self, user, batch_id, confirmation_id, expected_revision,
                      expected_quote_expires_at):
        if not _BATCH_RE.fullmatch(str(batch_id or "")):
            raise APIError(400, "视频方案编号无效", "invalid_batch")
        if not _REQUEST_RE.fullmatch(str(confirmation_id or "")):
            raise APIError(400, "确认编号无效", "invalid_confirmation")
        revision = self._expected_revision(expected_revision)
        quote_expires_at = self._expected_quote_expiry(expected_quote_expires_at)
        current = self.store.batch(user["username"], batch_id, include_private=True)
        job_count = len(current.get("jobs") or []) if current else 0
        try:
            batch = self.store.claim_confirmation(
                user["username"], batch_id, confirmation_id, revision,
                quote_expires_at,
                now=int(self.clock()),
                safety_margin_seconds=self._quote_safety_margin(job_count),
            )
        except (QuoteExpired, StateConflict, IdempotencyConflict, StoreError) as exc:
            self._raise_store_conflict(exc)
        if not batch.get("claimed_jobs"):
            return self.store.batch(user["username"], batch_id)
        for job in batch.get("claimed_jobs") or []:
            try:
                result = self._submit_matrix_job(user, job)
                self.store.finish_submit_claim(
                    user["username"], job["id"], job["revision"], **result,
                )
            except APIError as exc:
                self.store.finish_submit_claim(
                    user["username"], job["id"], job["revision"],
                    status="submission_unknown" if self._submission_uncertain(exc) else "failed_submission",
                    error=exc.detail,
                )
        self.store.update_workspace(
            user["username"], batch["project_id"], flow={"mode": "idle"},
        )
        return self.store.recompute_batch(user["username"], batch_id)

    def refresh_batch(self, user, batch_id):
        if not _BATCH_RE.fullmatch(str(batch_id or "")):
            raise APIError(400, "视频方案编号无效", "invalid_batch")
        batch = self.store.batch(user["username"], batch_id, include_private=True)
        if not batch:
            raise APIError(404, "视频方案不存在", "not_found")
        for job in self.store.claim_recovery(
            user["username"], batch_id, now=int(self.clock()),
        ):
            try:
                result = self._recover_matrix_job(user, job)
                self.store.finish_submit_claim(
                    user["username"], job["id"], job["revision"], **result,
                )
            except APIError as exc:
                self.store.finish_submit_claim(
                    user["username"], job["id"], job["revision"],
                    status="submission_unknown" if self._submission_uncertain(exc) else "failed_submission",
                    error=exc.detail,
                )
        batch = self.store.batch(user["username"], batch_id, include_private=True)
        for job in batch["jobs"]:
            if not job.get("job_id") or job.get("status") not in _RUNNING:
                continue
            try:
                result = self.bridge.action(
                    user["account_id"], "task", {"job_id": job["job_id"]},
                )
                status = str(result.get("status") or job["status"])
                if status in _DONE:
                    status = "done"
                elif status in _FAILED:
                    status = "failed"
                else:
                    status = "running"
                self.store.finish_task_poll(
                    user["username"], job["id"], job["revision"], status=status,
                    result=_public(result), error=str(result.get("error") or "")[:500],
                    refund_status=str(result.get("refund_status") or ""),
                )
            except APIError:
                continue
        return self.store.recompute_batch(user["username"], batch_id)

    def message(self, user, headers, body):
        if not isinstance(body, dict) or set(body) - {
            "message", "request_id", "intent", "payload", "project_id",
        }:
            raise APIError(400, "消息字段不合法", "invalid_request")
        message = str(body.get("message") or "").strip()
        request_id = str(body.get("request_id") or "")
        intent = str(body.get("intent") or "").strip()
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
        if not message or len(message) > 4000 or not _REQUEST_RE.fullmatch(request_id):
            raise APIError(400, "消息或 request_id 不合法", "invalid_request")
        self._gate(user)
        projects, project, workspace = self._ensure_project(user, headers)
        project_id = project["id"]
        requested_project = str(body.get("project_id") or "")
        if requested_project and requested_project != project_id:
            raise APIError(409, "当前画像项目已经切换，请刷新后重试", "project_conflict")
        request_hash = self._message_request_hash(
            project_id, message, intent, payload,
        )
        with self._lock(user["username"], project_id):
            try:
                user_message, created = self.store.add_message(
                    user["username"], project_id, "user", message,
                    request_id=request_id, request_hash=request_hash,
                )
            except IdempotencyConflict as exc:
                raise APIError(
                    409, "request_id 已绑定其他消息内容",
                    "idempotency_conflict",
                ) from exc
            self._request_context.username = user["username"]
            self._request_context.message_id = user_message["id"]
            if not created and user_message.get("public", {}).get("response"):
                self._request_context.username = ""
                self._request_context.message_id = 0
                return user_message["public"]["response"]
            if not created and intent not in {"confirm_plan", "confirm_payment"}:
                self._request_context.username = ""
                self._request_context.message_id = 0
                raise APIError(
                    409, "该请求正在处理或结果待确认，请刷新后重试",
                    "idempotency_in_progress",
                )
            if not intent:
                intent = self._intent_from_message(message)
            flow = workspace.get("flow") if isinstance(workspace.get("flow"), dict) else {}
            if intent == "regenerate_video" and flow.get("mode") == "template_review":
                intent = ""
            progress = self._progress(project)
            public = {}

            if not progress["foundation_ready"] or intent in {"profile_answer", "profile_choice"}:
                workspace, reply, public = self._profile_turn(
                    user, workspace, message, intent, payload,
                )
                project = self._local_project(workspace)
            elif flow.get("mode") == "profile_revision":
                overrides = dict(workspace.get("profile_overrides") or {})
                items = list(overrides.get("general") or [])
                items.append({"content": message[:1200], "created_at": int(self.clock())})
                overrides["general"] = items[-50:]
                profile = dict(workspace.get("profile") or {})
                profile["overrides"] = overrides["general"]
                workspace = self.store.update_workspace(
                    user["username"], project_id, profile_overrides=overrides,
                    profile=profile, flow={"mode": "idle"},
                )
                try:
                    acknowledgement = self.profile_agent.reply(profile, message)
                except ProfileAgentError as exc:
                    self._profile_model_error(exc)
                reply = "画像修改已保存。" + acknowledgement
                public = {"kind": "profile_override_saved", "profile": _public(profile)}
            elif intent == "modify_profile":
                self.store.update_workspace(
                    user["username"], project_id, flow={"mode": "profile_revision"},
                )
                reply = "请直接告诉我需要修改的画像内容。修改会生成新版本，不影响历史作品。"
                public = {
                    "kind": "profile_modules",
                    "options": ["定位诊断", "人设塑造", "价值主张", "故事资产"],
                }
            elif intent == "topic_plan":
                platforms = sanitize_platforms(payload.get("platforms")) or self._platforms_from_text(message)
                if not platforms:
                    selected = sanitize_platforms(workspace.get("platforms"))
                    self.store.update_workspace(
                        user["username"], project_id,
                        flow={"mode": "topic_platforms"},
                    )
                    reply = "这次选题和文案要适配哪些平台？可以多选。"
                    public = {
                        "kind": "platform_picker",
                        "platforms": [
                            {"id": key, "label": PLATFORM_LABELS[key]}
                            for key in ALLOWED_PLATFORMS
                        ],
                        "selected": selected,
                    }
                else:
                    workspace, reply, public = self._generate_topic_plan(
                        user, workspace, platforms, message,
                    )
            elif intent == "revise_copy":
                platforms = sanitize_platforms(workspace.get("platforms")) or ["douyin"]
                workspace, reply, public = self._generate_topic_plan(
                    user, workspace, platforms, "修改现有内容：" + message,
                )
            elif intent == "view_preferences":
                reply = self._preferences_reply(workspace)
                public = {"kind": "preferences"}
            elif intent == "clear_preferences":
                platform = str(payload.get("platform") or "")
                if platform not in ALLOWED_PLATFORMS:
                    mentioned = self._platforms_from_text(message)
                    platform = mentioned[0] if len(mentioned) == 1 else ""
                preferences = clear_preferences(workspace.get("template_video_preferences"), platform)
                workspace = self.store.update_workspace(
                    user["username"], project_id,
                    template_video_preferences=preferences,
                )
                reply = (PLATFORM_LABELS[platform] + "偏好已清空。") if platform in ALLOWED_PLATFORMS else "模板视频偏好已全部清空。"
                public = {"kind": "preferences_cleared"}
            elif intent == "set_platforms":
                platforms = sanitize_platforms(payload.get("platforms"))
                if not platforms:
                    raise APIError(400, "请至少选择一个平台", "platform_required")
                flow = dict(flow)
                flow["platforms"] = platforms
                workspace = self.store.update_workspace(
                    user["username"], project_id, platforms=platforms, flow=flow,
                )
                if flow.get("mode") == "topic_platforms":
                    workspace, reply, public = self._generate_topic_plan(
                        user, workspace, platforms,
                        "请根据我的画像生成完整选题计划和可发布文案",
                    )
                elif flow.get("mode") == "template_platforms":
                    workspace = self.store.update_workspace(
                        user["username"], project_id, platforms=platforms,
                        flow={
                            "mode": "template_collect", "platforms": platforms,
                            "topic": flow.get("topic") or "",
                        },
                    )
                    if flow.get("topic"):
                        reply, public = self._create_video_plan(
                            user, project, workspace, flow["topic"], platforms,
                        )
                    else:
                        reply = "已选择%s。请告诉我这次视频的主题或核心观点。" % "、".join(
                            PLATFORM_LABELS[key] for key in platforms
                        )
                        public = {"kind": "topic_prompt"}
                elif flow.get("mode") == "template_collect" and flow.get("topic"):
                    reply, public = self._create_video_plan(
                        user, project, workspace, flow["topic"], platforms,
                    )
                else:
                    reply = "已选择%s。请告诉我这次视频的主题或核心观点。" % "、".join(PLATFORM_LABELS[key] for key in platforms)
                    public = {"kind": "topic_prompt"}
            elif intent == "start_video":
                reply, public = self._start_video(user, project, workspace, message, payload)
            elif intent == "adjust_video_platforms":
                batch_id = str(payload.get("batch_id") or flow.get("batch_id") or "")
                batch = self.store.batch(user["username"], batch_id, include_private=True)
                if not batch or batch["status"] not in {"draft", "ready", "quoted"}:
                    raise APIError(409, "当前方案不能调整平台", "invalid_state")
                if batch["revision"] != self._expected_revision(payload.get("expected_revision")):
                    raise APIError(409, "方案版本已经变化，请刷新后重试", "state_conflict")
                selected = [item.get("platform") for item in batch.get("plans") or []]
                self.store.update_workspace(
                    user["username"], project_id,
                    flow={
                        "mode": "template_platforms", "topic": batch.get("topic") or "",
                        "platforms": selected,
                    },
                )
                reply = "请选择这版视频需要的平台，可以多选。"
                public = {
                    "kind": "platform_picker",
                    "platforms": [
                        {"id": key, "label": PLATFORM_LABELS[key]}
                        for key in ALLOWED_PLATFORMS
                    ],
                    "selected": selected,
                }
            elif intent == "regenerate_video":
                previous = self.store.latest_batch(
                    user["username"], project_id, include_private=True,
                )
                if not previous:
                    reply, public = self._start_video(user, project, workspace, message, payload)
                elif previous["status"] in _RUNNING or previous["status"] in {"quoted", "submitting"}:
                    raise APIError(409, "上一批视频仍在处理中，请先等待结果", "batch_in_progress")
                else:
                    reply, public = self._new_video_version(
                        user, project, workspace, previous, message,
                    )
            elif intent == "confirm_plan":
                batch_id = str(payload.get("batch_id") or flow.get("batch_id") or "")
                batch = self.quote_batch(user, batch_id, payload.get("expected_revision"))
                reply = "报价已生成。请核对各平台明细和总价，确认后才会扣点并分别创建任务。"
                public = {
                    "kind": "video_quote", "batch": batch,
                    "actions": [
                        {"intent": "adjust_video_platforms", "label": "调整平台"},
                        {"intent": "confirm_payment", "label": "确认扣点并开始生成", "primary": True},
                    ],
                }
            elif intent == "confirm_payment":
                batch_id = str(payload.get("batch_id") or flow.get("batch_id") or "")
                confirmation_id = str(payload.get("confirmation_id") or "")
                try:
                    batch = self.confirm_batch(
                        user, batch_id, confirmation_id, payload.get("expected_revision"),
                        payload.get("expected_quote_expires_at"),
                    )
                except APIError as exc:
                    if exc.code != "quote_expired":
                        raise
                    batch = self.quote_batch(
                        user, batch_id, payload.get("expected_revision"),
                    )
                    reply = "原报价已过期或剩余时间不足，已自动重新报价。请再次核对后确认扣点。"
                    public = {
                        "kind": "video_quote", "batch": batch,
                        "actions": [
                            {"intent": "adjust_video_platforms", "label": "调整平台"},
                            {"intent": "confirm_payment", "label": "确认扣点并开始生成", "primary": True},
                        ],
                    }
                else:
                    reply = "已按平台分别提交任务。你可以在右侧查看每个任务的进度和结果。"
                    public = {"kind": "video_submitted", "batch": batch}
                    self.store.update_workspace(user["username"], project_id, flow={"mode": "idle"})
            elif flow.get("mode") == "template_collect":
                topic = str(flow.get("topic") or self._topic_from_text(message)).strip()[:400]
                platforms = sanitize_platforms(flow.get("platforms") or workspace.get("platforms"))
                if not platforms:
                    platforms = self._platforms_from_text(message)
                    if platforms:
                        workspace = self.store.update_workspace(
                            user["username"], project_id, platforms=platforms,
                            flow={**flow, "platforms": platforms},
                        )
                if not platforms:
                    reply, public = self._start_video(
                        user, project, workspace, message, {"topic": topic},
                    )
                elif not topic:
                    reply = "已选择%s。请告诉我这次视频的主题或核心观点。" % "、".join(
                        PLATFORM_LABELS[key] for key in platforms
                    )
                    public = {"kind": "topic_prompt"}
                else:
                    reply, public = self._create_video_plan(
                        user, project, workspace, topic, platforms,
                    )
            elif flow.get("mode") == "template_review":
                batch = self.store.batch(
                    user["username"], str(flow.get("batch_id") or ""), include_private=True,
                )
                if not batch:
                    raise APIError(409, "当前视频方案已失效，请重新开始", "batch_stale")
                preferences, changed = remember_preference(
                    workspace.get("template_video_preferences"), message,
                )
                if changed:
                    workspace = self.store.update_workspace(
                        user["username"], project_id,
                        template_video_preferences=preferences,
                    )
                reply, public = self._revise_video_plan(user, workspace, batch, message)
            else:
                preferences, changed = remember_preference(
                    workspace.get("template_video_preferences"), message,
                )
                if changed:
                    workspace = self.store.update_workspace(
                        user["username"], project_id,
                        template_video_preferences=preferences,
                    )
                try:
                    reply = self.profile_agent.reply(workspace.get("profile") or {}, message)
                except ProfileAgentError as exc:
                    self._profile_model_error(exc)
                public = {
                    "kind": "assistant_reply",
                    "actions": [
                        {"intent": "start_video", "label": "开始制作视频"},
                        {"intent": "topic_plan", "label": "生成选题计划"},
                        {"intent": "modify_profile", "label": "修改我的画像"},
                    ],
                }

            self._record_assistant(user, project_id, reply, public)
            response = self._response(user, headers, project_id, reply, public)
            self.store.update_message_public(
                user["username"], user_message["id"], {"response": response},
            )
            self._request_context.username = ""
            self._request_context.message_id = 0
            return response


class CreatorAgentHandler(BaseHTTPRequestHandler):
    service = None
    server_version = "HuangqueCreatorAgent/2.0"

    def log_message(self, format_string, *args):
        return

    def _send(self, status, value):
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self, maximum=128 * 1024):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise APIError(400, "Content-Length 不合法", "invalid_request") from exc
        if length < 0 or length > maximum:
            raise APIError(413, "请求体过大", "request_too_large")
        try:
            value = json.loads(self.rfile.read(length) or b"{}")
        except (UnicodeDecodeError, ValueError) as exc:
            raise APIError(400, "请求体不是合法 JSON", "invalid_json") from exc
        if not isinstance(value, dict):
            raise APIError(400, "请求体必须是对象", "invalid_json")
        return value

    def _user(self):
        return self.service.auth.verify(self.headers)

    def _route(self):
        return urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"

    def _handle(self, method):
        path = self._route()
        if method == "GET" and path == "/health":
            return self._send(200, self.service.health())
        user = self._user()
        if method == "GET" and path == "/capability":
            return self._send(200, self.service.capability(user))
        if method == "GET" and path == "/bootstrap":
            return self._send(200, self.service.bootstrap(user, self.headers))
        if method == "POST" and path == "/messages":
            return self._send(200, self.service.message(user, self.headers, self._body()))
        match = re.fullmatch(r"/projects/([0-9a-f]{12})/(select|rename)", path)
        if method == "POST" and match:
            project_id, action = match.groups()
            if action == "select":
                return self._send(200, self.service.select_project(user, self.headers, project_id))
            return self._send(200, {"workspace": self.service.rename_project(
                user, self.headers, project_id, self._body(),
            )})
        match = re.fullmatch(r"/batches/(creator_batch_[0-9a-f]{32})/(quote|confirm|refresh)", path)
        if method == "POST" and match:
            batch_id, action = match.groups()
            body = self._body()
            if action == "quote":
                return self._send(200, {"batch": self.service.quote_batch(
                    user, batch_id, body.get("expected_revision"),
                )})
            if action == "confirm":
                return self._send(200, {"batch": self.service.confirm_batch(
                    user, batch_id, str(body.get("confirmation_id") or ""),
                    body.get("expected_revision"), body.get("expected_quote_expires_at"),
                )})
            return self._send(200, {"batch": self.service.refresh_batch(user, batch_id)})
        raise APIError(404, "not found", "not_found")

    def do_GET(self):
        try:
            self._handle("GET")
        except APIError as exc:
            self._send(exc.status, {"detail": exc.detail, "code": exc.code})
        except Exception as exc:
            print("[creator-agent] GET %s failed: %s" % (self._route(), type(exc).__name__), file=sys.stderr, flush=True)
            self._send(500, {"detail": "AI 创作助手暂不可用", "code": "internal_error"})

    def do_POST(self):
        try:
            self._handle("POST")
        except APIError as exc:
            self._send(exc.status, {"detail": exc.detail, "code": exc.code})
        except StoreError:
            self._send(409, {"detail": "页面状态已变化，请刷新后重试", "code": "state_conflict"})
        except Exception as exc:
            print("[creator-agent] POST %s failed: %s" % (self._route(), type(exc).__name__), file=sys.stderr, flush=True)
            self._send(500, {"detail": "AI 创作助手暂不可用", "code": "internal_error"})


def build_service(environment=None):
    environment = environment or os.environ
    store = CreatorAgentStore(
        environment.get("CREATOR_AGENT_DB", "/var/lib/huangque-creator-agent/creator_agent.db")
    )
    planner = CreatorPlanner.from_environment(environment)
    auth_url = environment.get("CREATOR_AGENT_AUTH_URL", "http://127.0.0.1:8095")
    auth = AuthClient(auth_url)
    bridge = BridgeClient(
        auth_url,
        environment.get("CREATOR_AGENT_INTERNAL_TOKEN")
        or environment.get("HQ_INTERNAL_TOKEN")
        or environment.get("AUTH_INTERNAL_TOKEN")
        or environment.get("INTERNAL_TOKEN")
        or "",
    )
    profile_agent = DeepSeekProfileAgent(
        environment.get("CREATOR_AGENT_API_KEY") or environment.get("DEEPSEEK_API_KEY") or "",
        base_url=environment.get("CREATOR_AGENT_BASE_URL", "https://api.deepseek.com"),
        model=environment.get("CREATOR_AGENT_MODEL", "deepseek-v4-flash"),
    )
    return CreatorAgentService(store, planner, auth, bridge, profile_agent)


def serve(host="127.0.0.1", port=8114, service=None):
    CreatorAgentHandler.service = service or build_service()
    server = ThreadingHTTPServer((host, int(port)), CreatorAgentHandler)
    server.serve_forever()
