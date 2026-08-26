"""HTTP and orchestration boundary for the independent AI Creator Agent."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .planner import (
    ALLOWED_PLATFORMS,
    PLATFORM_LABELS,
    CreatorPlanner,
    clear_preferences,
    remember_preference,
    sanitize_platforms,
    sanitize_preferences,
)
from .store import CreatorAgentStore, StoreError


_PROJECT_RE = re.compile(r"^[0-9a-f]{12}$")
_BATCH_RE = re.compile(r"^creator_batch_[0-9a-f]{32}$")
_REQUEST_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_PRIVATE_KEYS = {"quote_token", "job_id", "idempotency_key", "confirmation_id"}
_RUNNING = {"submitted", "queued", "running", "verifying", "processing", "submission_unknown"}
_FAILED = {"error", "failed", "refunded", "failed_submission"}
_DONE = {"done", "completed", "success"}


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


class JSONClient:
    def __init__(self, base_url, timeout=30, opener=None):
        self.base_url = str(base_url or "").rstrip("/")
        self.timeout = int(timeout)
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method, path, headers, body=None, timeout=None):
        forwarded = _auth_headers(headers)
        forwarded["Accept"] = "application/json"
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            forwarded["Content-Type"] = "application/json"
        request = urllib.request.Request(
            _url(self.base_url, path), data=data, headers=forwarded, method=method,
        )
        try:
            with self.opener.open(request, timeout=timeout or self.timeout) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                status = response.getcode()
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(4 * 1024 * 1024 + 1), exc.code
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise APIError(503, "IP12 服务暂不可用", "ip12_unavailable") from exc
        if len(raw) > 4 * 1024 * 1024:
            raise APIError(502, "IP12 响应过大", "ip12_response_too_large")
        try:
            value = json.loads(raw or b"{}")
        except (TypeError, ValueError) as exc:
            raise APIError(502, "IP12 返回了无效响应", "ip12_invalid_response") from exc
        if not 200 <= int(status) < 300:
            detail = value.get("detail") or value.get("error") or "IP12 请求失败"
            code = value.get("code") or "ip12_error"
            raise APIError(status, detail, code)
        return value, int(status)


class IP12Client(JSONClient):
    def health(self):
        value, _ = self.request("GET", "/healthz", {}, timeout=3)
        return value

    def projects(self, headers):
        value, _ = self.request("GET", "/api/conversations", headers)
        return value if isinstance(value, list) else value.get("items") or []

    def create(self, headers, title="我的个人画像"):
        value, _ = self.request("POST", "/api/conversations", headers, {"title": title})
        return value

    def project(self, headers, project_id):
        value, _ = self.request("GET", "/api/conversations/" + project_id, headers)
        return value

    def turn(self, headers, project_id, *, message="", action=None,
             expected_revision=None, request_id="", foundation_review=""):
        body = {"conversation_id": project_id, "request_id": request_id}
        if action is not None:
            body["action"] = action
        else:
            body["message"] = message
        if expected_revision is not None:
            body["expected_revision"] = expected_revision
        if foundation_review:
            body["foundation_review"] = foundation_review
        value, status = self.request("POST", "/api/chat-complete", headers, body, timeout=300)
        value["http_status"] = status
        return value

    def generate_foundation(self, headers, project_id):
        value, _ = self.request(
            "POST", "/api/foundation-report/generate", headers,
            {"conversation_id": project_id}, timeout=180,
        )
        return value

    def confirm_foundation(self, headers, project_id, revision, report_id):
        value, _ = self.request(
            "POST", "/api/foundation-report/confirm", headers, {
                "conversation_id": project_id,
                "expected_revision": revision,
                "report_id": report_id,
            }, timeout=60,
        )
        return value


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


class CreatorAgentService:
    def __init__(self, store, planner, auth, bridge, ip12):
        self.store = store
        self.planner = planner
        self.auth = auth
        self.bridge = bridge
        self.ip12 = ip12
        self._locks = {}
        self._locks_guard = threading.Lock()

    def _lock(self, username, project_id):
        key = username + ":" + project_id
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    def health(self):
        checks = {
            "bridge_token": bool(getattr(self.bridge, "internal_token", "")),
            "auth_url": _valid_loopback_base(getattr(self.auth, "base_url", "")),
            "ip12_url": _valid_loopback_base(getattr(self.ip12, "base_url", "")),
            "database_writable": bool(self.store.health()),
            "bridge_catalog": False,
            "ip12_reachable": False,
        }
        bridge_detail = ""
        ip12_detail = ""
        if checks["bridge_token"] and checks["auth_url"]:
            try:
                bridge = self.bridge.health()
                actions = set(bridge.get("actions") or []) if isinstance(bridge, dict) else set()
                checks["bridge_catalog"] = bool(
                    bridge.get("ready") is True
                    and {"matrix-template-templates", "matrix-template-generate"}.issubset(actions)
                )
            except APIError as exc:
                bridge_detail = exc.code
        if checks["ip12_url"]:
            try:
                checks["ip12_reachable"] = self.ip12.health().get("ok") is True
            except APIError as exc:
                ip12_detail = exc.code
        ready = all(checks.values())
        return {
            "ok": True,
            "ready": ready,
            "service": "huangque-creator-agent",
            "version": 2,
            "checks": checks,
            "details": {
                key: value for key, value in {
                    "bridge_catalog": bridge_detail,
                    "ip12_reachable": ip12_detail,
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
        projects = self.ip12.projects(headers)
        if not projects:
            self.ip12.create(headers, "我的个人画像")
            projects = self.ip12.projects(headers)
        if not projects:
            raise APIError(502, "画像项目创建失败", "project_create_failed")
        owned = {str(item.get("id") or ""): item for item in projects if isinstance(item, dict)}
        selected = self.store.active_project(user["username"])
        if selected not in owned:
            selected = next(iter(owned))
            self.store.set_active_project(user["username"], selected)
        item = owned[selected]
        workspace = self.store.ensure_workspace(
            user["username"], selected, str(item.get("title") or "我的个人画像"),
        )
        project = self.ip12.project(headers, selected)
        self.store.sync_ip12_messages(user["username"], selected, project.get("messages") or [])
        return projects, project, workspace

    @staticmethod
    def _state(project):
        return project.get("coach_state") if isinstance(project.get("coach_state"), dict) else {}

    @classmethod
    def _foundation_ready(cls, project):
        state = cls._state(project)
        report = state.get("foundation_report") if isinstance(state.get("foundation_report"), dict) else {}
        completed = {int(item) for item in state.get("completed_modules") or [] if str(item).isdigit()}
        return (
            {1, 2, 3, 4}.issubset(completed)
            and report.get("status") == "confirmed"
            and report.get("review_status") != "dirty"
        )

    @classmethod
    def _progress(cls, project):
        state = cls._state(project)
        completed = sorted({
            int(item) for item in state.get("completed_modules") or []
            if str(item).isdigit() and 1 <= int(item) <= 6
        })
        report = state.get("foundation_report") if isinstance(state.get("foundation_report"), dict) else {}
        return {
            "current_module": int(state.get("current_module") or 1),
            "module_step": int(state.get("module_step") or 0),
            "completed_modules": completed,
            "foundation_status": str(report.get("status") or "missing"),
            "foundation_report_id": str(report.get("report_id") or ""),
            "foundation_ready": cls._foundation_ready(project),
            "profile_complete": cls._foundation_ready(project),
        }

    @classmethod
    def _ip12_context(cls, project, workspace=None):
        state = cls._state(project)
        profile = state.get("ip_profile") if isinstance(state.get("ip_profile"), dict) else {}
        return {
            "ip_profile": _public(profile),
            "completed_modules": cls._progress(project)["completed_modules"],
            "foundation_confirmed": cls._foundation_ready(project),
            "creator_profile_overrides": _public((workspace or {}).get("profile_overrides") or {}),
        }

    @classmethod
    def _public_project(cls, project, workspace):
        progress = cls._progress(project)
        report = cls._state(project).get("foundation_report") or {}
        return {
            "id": project.get("id"),
            "title": project.get("title") or "我的个人画像",
            "display_name": workspace.get("alias") or project.get("title") or "我的个人画像",
            "updated": project.get("updated") or "",
            "revision": cls._state(project).get("revision"),
            "progress": progress,
            "harness_actions": cls._safe_ip12_actions(project.get("harness_actions") or []),
            "reports": _public(project.get("reports") or {}),
            "deliverables": _public(project.get("deliverables") or {}),
            "artifacts": _public(project.get("artifacts") or []),
            "foundation_pdf_url": (
                "/workbench/ip12/api/foundation-report/%s.pdf?preview=1" % project.get("id")
                if report.get("status") in {"awaiting_confirmation", "confirmed"} else ""
            ),
        }

    @staticmethod
    def _safe_ip12_actions(actions):
        allowed = {
            "confirm_intake", "edit_intake", "confirm_checkpoint", "edit_checkpoint",
            "select_checkpoint_choice", "resume_choice_generation",
        }
        return _public([
            item for item in actions or []
            if isinstance(item, dict) and item.get("type") in allowed
        ])

    def _project_list(self, username, projects, active_id, workspace):
        result = []
        for item in projects:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            item_workspace = (
                workspace if item["id"] == active_id
                else self.store.workspace(username, item["id"])
            ) or {}
            result.append({
                "id": item["id"],
                "title": item_workspace.get("alias") or item.get("title") or "我的个人画像",
                "updated": item.get("updated") or "",
                "active": item["id"] == active_id,
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
        projects = self.ip12.projects(headers)
        if project_id not in {str(item.get("id") or "") for item in projects if isinstance(item, dict)}:
            raise APIError(404, "画像项目不存在", "not_found")
        self.store.set_active_project(user["username"], project_id)
        project = self.ip12.project(headers, project_id)
        workspace = self.store.ensure_workspace(
            user["username"], project_id, project.get("title") or "我的个人画像",
        )
        self.store.sync_ip12_messages(user["username"], project_id, project.get("messages") or [])
        return self._snapshot(user, headers, projects, project, workspace)

    def rename_project(self, user, headers, project_id, body):
        self._gate(user)
        project_id = self._project_id(project_id)
        project = self.ip12.project(headers, project_id)
        alias = re.sub(r"\s+", " ", str((body or {}).get("title") or "")).strip()[:120]
        if not alias:
            raise APIError(400, "画像名称不能为空", "invalid_title")
        self.store.ensure_workspace(user["username"], project_id, project.get("title") or alias)
        return self.store.update_workspace(user["username"], project_id, alias=alias)

    @staticmethod
    def _clean_ip12_action(value):
        if not isinstance(value, dict):
            raise APIError(400, "IP12 操作无效", "invalid_action")
        allowed = {"type", "target_id", "choice_id"}
        result = {key: str(value.get(key) or "")[:160] for key in allowed if value.get(key)}
        if result.get("type") not in {
            "confirm_intake", "edit_intake", "confirm_checkpoint", "edit_checkpoint",
            "select_checkpoint_choice", "resume_choice_generation",
        }:
            raise APIError(400, "IP12 操作类型无效", "invalid_action")
        if not result.get("target_id"):
            raise APIError(400, "IP12 操作目标无效", "invalid_action")
        return result

    def _record_assistant(self, user, project_id, content, public=None):
        self.store.add_message(
            user["username"], project_id, "assistant", content, public=public or {},
        )

    def _response(self, user, headers, project_id, reply, public=None):
        projects = self.ip12.projects(headers)
        project = self.ip12.project(headers, project_id)
        workspace = self.store.workspace(user["username"], project_id)
        snapshot = self._snapshot(user, headers, projects, project, workspace)
        return {"reply": reply, "message_public": _public(public or {}), **snapshot}

    def _ip12_turn(self, user, headers, project, message, request_id,
                   action=None, foundation_review="", suppress_production=False):
        state = self._state(project)
        result = self.ip12.turn(
            headers, project["id"], message=message, action=action,
            expected_revision=state.get("revision"), request_id=request_id,
            foundation_review=foundation_review,
        )
        latest = self.ip12.project(headers, project["id"])
        reply = str(result.get("assistant") or "已更新画像进度。")
        if suppress_production:
            marker = "六步已经完成，数字人口播能力已经解锁。"
            if marker in reply:
                reply = reply.split(marker, 1)[0].rstrip() or "选题与文案已经完成并保存。"
        return latest, reply, {
            "source": "ip12",
            "actions": self._safe_ip12_actions(result.get("actions") or []),
            "new_completed": result.get("new_completed") or [],
            "foundation_report": result.get("foundation_report"),
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

    @staticmethod
    def _profile_override_section(message):
        text = str(message or "")
        if re.search(r"定位|赛道|技能|受众|客户|经历|兴趣", text):
            return "module_1"
        if re.search(r"人设|性格|表达|风格|语气", text):
            return "module_2"
        if re.search(r"价值|主张|优势|使命|目标", text):
            return "module_3"
        if re.search(r"故事|案例|挫折|成长|愿景", text):
            return "module_4"
        return "general"

    def _record_profile_override(self, user, project_id, workspace, message):
        overrides = workspace.get("profile_overrides")
        overrides = dict(overrides) if isinstance(overrides, dict) else {}
        section = self._profile_override_section(message)
        items = list(overrides.get(section) or [])[-19:]
        items.append({
            "content": re.sub(r"\s+", " ", str(message or "")).strip()[:1000],
            "updated_at": int(time.time()),
        })
        overrides[section] = items
        return self.store.update_workspace(
            user["username"], project_id, profile_overrides=overrides,
            flow={"mode": "idle"},
        )

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
        planned = self.planner.video_plan(
            self._ip12_context(project, workspace), topic, platforms, templates,
            workspace.get("template_video_preferences"),
        )
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
        revised = self.planner.revise_video_plan(
            batch.get("plans") or [], instruction, templates,
            workspace.get("template_video_preferences"),
        )
        updated = self.store.replace_batch_plans(
            user["username"], batch["id"], revised["platform_plans"],
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
        revised = self.planner.revise_video_plan(
            source_plans, instruction, templates,
            workspace.get("template_video_preferences"),
        )
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

    def quote_batch(self, user, batch_id):
        batch_id = str(batch_id or "")
        if not _BATCH_RE.fullmatch(batch_id):
            raise APIError(400, "视频方案编号无效", "invalid_batch")
        batch = self.store.batch(user["username"], batch_id, include_private=True)
        if not batch:
            raise APIError(404, "视频方案不存在", "not_found")
        if batch["status"] not in {"draft", "ready", "quoted"}:
            raise APIError(409, "当前方案不能重新报价", "invalid_state")
        if batch["status"] == "quoted" and all(job.get("quote_token") for job in batch["jobs"]):
            return self.store.batch(user["username"], batch_id)
        items, total, points, expires = [], 0, None, None
        quoted_jobs = []
        try:
            for job in batch["jobs"]:
                result = self.bridge.action(
                    user["account_id"], "matrix-template-generate", job["input"], confirm=False,
                )
                token = str(result.get("quote_token") or "")
                if not token:
                    raise APIError(502, "模板视频没有返回有效报价", "quote_invalid")
                public_quote = _public(result)
                updated = self.store.update_job(
                    user["username"], job["id"], status="quoted",
                    quote_token=token, quote=public_quote, error="",
                )
                quoted_jobs.append(updated)
                cost = int(public_quote.get("cost") or 0)
                if cost <= 0:
                    raise APIError(502, "模板视频报价无效", "quote_invalid")
                total += cost
                try:
                    current_points = int(public_quote.get("points"))
                except (TypeError, ValueError):
                    current_points = None
                try:
                    current_expires = int(public_quote.get("expires_in"))
                except (TypeError, ValueError):
                    current_expires = None
                if current_points is not None:
                    points = current_points if points is None else min(points, current_points)
                if current_expires is not None:
                    expires = current_expires if expires is None else min(expires, current_expires)
                items.append({
                    "platform": job["platform"], "label": PLATFORM_LABELS[job["platform"]],
                    "cost": cost,
                })
        except APIError:
            for job in quoted_jobs:
                self.store.update_job(
                    user["username"], job["id"], status="draft", quote_token="", quote={},
                )
            raise
        quote = {
            "items": items, "total_cost": total, "points": points,
            "expires_in": expires, "confirmation_required": True,
        }
        self.store.update_batch(user["username"], batch_id, status="quoted", quote=quote)
        return self.store.batch(user["username"], batch_id)

    @staticmethod
    def _batch_status(jobs):
        statuses = [job.get("status") for job in jobs]
        if statuses and all(status in _DONE for status in statuses):
            return "done"
        if statuses and all(status in _FAILED for status in statuses):
            return "failed"
        if any(status in _RUNNING for status in statuses):
            return "running"
        if any(status in _DONE for status in statuses) and any(status in _FAILED for status in statuses):
            return "partial"
        return "submitted"

    @staticmethod
    def _submission_uncertain(error):
        return error.status >= 500 or error.code in {
            "bridge_unavailable", "upstream_unavailable", "idempotency_in_progress",
            "result_unknown", "submit_result_unknown", "cli_internal_error",
        }

    def _submit_matrix_job(self, user, job, confirmation_id):
        result = self.bridge.action(
            user["account_id"], "matrix-template-generate", job["input"],
            confirm=True, quote_token=job["quote_token"],
            idempotency_key=job["idempotency_key"],
        )
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
        return self.store.update_job(
            user["username"], job["id"], status=status,
            confirmation_id=confirmation_id + ":" + job["platform"],
            job_id=provider_job, result=_public(result), error="",
        )

    def confirm_batch(self, user, batch_id, confirmation_id):
        if not _BATCH_RE.fullmatch(str(batch_id or "")):
            raise APIError(400, "视频方案编号无效", "invalid_batch")
        if not _REQUEST_RE.fullmatch(str(confirmation_id or "")):
            raise APIError(400, "确认编号无效", "invalid_confirmation")
        batch = self.store.batch(user["username"], batch_id, include_private=True)
        if not batch:
            raise APIError(404, "视频方案不存在", "not_found")
        if batch.get("confirmation_id"):
            if batch["confirmation_id"] != confirmation_id:
                raise APIError(409, "该方案已绑定另一笔确认", "confirmation_conflict")
            if not any(
                job.get("status") in {"quoted", "submission_unknown"}
                for job in batch["jobs"]
            ):
                return self.store.batch(user["username"], batch_id)
        elif batch["status"] != "quoted" or not all(job.get("quote_token") for job in batch["jobs"]):
            raise APIError(409, "请先取得完整报价", "quote_required")
        if not batch.get("confirmation_id"):
            self.store.update_batch(
                user["username"], batch_id, status="submitting", confirmation_id=confirmation_id,
            )
        for job in batch["jobs"]:
            if job.get("status") not in {"quoted", "submission_unknown"}:
                continue
            try:
                self._submit_matrix_job(user, job, confirmation_id)
            except APIError as exc:
                self.store.update_job(
                    user["username"], job["id"],
                    status="submission_unknown" if self._submission_uncertain(exc) else "failed_submission",
                    confirmation_id=confirmation_id + ":" + job["platform"], error=exc.detail,
                )
        current = self.store.batch(user["username"], batch_id, include_private=True)
        self.store.update_batch(
            user["username"], batch_id, status=self._batch_status(current["jobs"]),
        )
        self.store.update_workspace(
            user["username"], batch["project_id"], flow={"mode": "idle"},
        )
        return self.store.batch(user["username"], batch_id)

    def refresh_batch(self, user, batch_id):
        if not _BATCH_RE.fullmatch(str(batch_id or "")):
            raise APIError(400, "视频方案编号无效", "invalid_batch")
        batch = self.store.batch(user["username"], batch_id, include_private=True)
        if not batch:
            raise APIError(404, "视频方案不存在", "not_found")
        for job in batch["jobs"]:
            if job.get("status") == "submission_unknown" and not job.get("job_id"):
                try:
                    self._submit_matrix_job(
                        user, job, batch.get("confirmation_id") or "creator-recovery",
                    )
                except APIError as exc:
                    if not self._submission_uncertain(exc):
                        self.store.update_job(
                            user["username"], job["id"], status="failed_submission",
                            error=exc.detail,
                        )
                continue
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
                self.store.update_job(
                    user["username"], job["id"], status=status,
                    result=_public(result), error=str(result.get("error") or "")[:500],
                    refund_status=str(result.get("refund_status") or ""),
                )
            except APIError:
                continue
        current = self.store.batch(user["username"], batch_id, include_private=True)
        self.store.update_batch(
            user["username"], batch_id, status=self._batch_status(current["jobs"]),
        )
        return self.store.batch(user["username"], batch_id)

    def message(self, user, headers, body):
        if not isinstance(body, dict) or set(body) - {"message", "request_id", "intent", "payload"}:
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
        with self._lock(user["username"], project_id):
            user_message, created = self.store.add_message(
                user["username"], project_id, "user", message, request_id=request_id,
            )
            if not created and user_message.get("public", {}).get("response"):
                return user_message["public"]["response"]
            if not created:
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

            if intent == "confirm_foundation":
                state = self._state(project)
                report = state.get("foundation_report") or {}
                if report.get("status") != "awaiting_confirmation" or not report.get("report_id"):
                    raise APIError(409, "请先生成并查看最新定位画像 PDF", "foundation_not_ready")
                self.ip12.confirm_foundation(
                    headers, project_id, state.get("revision"), report.get("report_id"),
                )
                reply = "定位画像已确认。现在可以开始制作视频、生成选题计划，或继续修改画像。"
                public = {"kind": "foundation_confirmed"}
                self.store.update_workspace(user["username"], project_id, flow={"mode": "idle"})
            elif (
                flow.get("mode") == "profile_revision"
                and (self._state(project).get("foundation_report") or {}).get("status") == "confirmed"
                and intent != "ip12_action"
            ):
                workspace = self._record_profile_override(
                    user, project_id, workspace, message,
                )
                reply = "画像补充已保存。后续内容和视频会优先采用这次更新，原定位 PDF 与历史作品保持不变。"
                public = {"kind": "profile_override_saved"}
            elif intent == "ip12_action" or not progress["foundation_ready"] or flow.get("mode") in {"topic_plan", "profile_revision"}:
                action = self._clean_ip12_action(payload.get("action")) if intent == "ip12_action" else None
                foundation_review = "revision" if flow.get("mode") == "profile_revision" and action is None else ""
                project, reply, public = self._ip12_turn(
                    user, headers, project, message, request_id,
                    action=action, foundation_review=foundation_review,
                    suppress_production=flow.get("mode") == "topic_plan",
                )
                latest_progress = self._progress(project)
                if 4 in latest_progress["completed_modules"] and latest_progress["foundation_status"] in {"missing", "failed"}:
                    try:
                        self.ip12.generate_foundation(headers, project_id)
                        project = self.ip12.project(headers, project_id)
                        public["foundation_generated"] = True
                        reply += "\n\n定位画像 PDF 已生成，请在右侧预览后确认。"
                    except APIError as exc:
                        project = self.ip12.project(headers, project_id)
                        if self._progress(project)["foundation_status"] not in {"awaiting_confirmation", "confirmed"}:
                            public["foundation_error"] = exc.detail
                latest_progress = self._progress(project)
                if flow.get("mode") == "topic_plan" and 6 in latest_progress["completed_modules"]:
                    self.store.update_workspace(user["username"], project_id, flow={"mode": "idle"})
            elif intent == "modify_profile":
                self.store.update_workspace(
                    user["username"], project_id, flow={"mode": "profile_revision"},
                )
                reply = "请告诉我需要修改定位、人设、价值主张还是故事资产，以及具体要改什么。"
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
                    workspace = self.store.update_workspace(
                        user["username"], project_id, platforms=platforms,
                        flow={"mode": "topic_plan", "platforms": platforms},
                    )
                    project, reply, public = self._ip12_turn(
                        user, headers, project,
                        message,
                        request_id,
                        suppress_production=True,
                    )
                    public["kind"] = "topic_plan"
            elif intent == "revise_copy":
                project, reply, public = self._ip12_turn(
                    user, headers, project, message, request_id,
                    suppress_production=True,
                )
                public["kind"] = "topic_plan"
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
                    workspace = self.store.update_workspace(
                        user["username"], project_id, platforms=platforms,
                        flow={"mode": "topic_plan", "platforms": platforms},
                    )
                    project, reply, public = self._ip12_turn(
                        user, headers, project,
                        message,
                        request_id,
                        suppress_production=True,
                    )
                    public["kind"] = "topic_plan"
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
                batch = self.quote_batch(user, batch_id)
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
                batch = self.confirm_batch(user, batch_id, confirmation_id)
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
                reply = self.planner.reply(self._ip12_context(project, workspace), message)
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
            if action == "quote":
                return self._send(200, {"batch": self.service.quote_batch(user, batch_id)})
            if action == "confirm":
                return self._send(200, {"batch": self.service.confirm_batch(
                    user, batch_id, str(self._body().get("confirmation_id") or ""),
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
    ip12 = IP12Client(environment.get("CREATOR_AGENT_IP12_URL", "http://127.0.0.1:3102"), timeout=300)
    return CreatorAgentService(store, planner, auth, bridge, ip12)


def serve(host="127.0.0.1", port=8114, service=None):
    CreatorAgentHandler.service = service or build_service()
    server = ThreadingHTTPServer((host, int(port)), CreatorAgentHandler)
    server.serve_forever()
