import ast
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
HERMES = ROOT / "server" / "hermes_ip12"


def extract_js_function(source, name):
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


class HermesIP12SourceTests(unittest.TestCase):
    def test_coach_prompt_finishes_ready_outputs_in_the_same_reply(self):
        prompt = (HERMES / "prompt.md").read_text(encoding="utf-8")
        self.assertIn("同一条回复", prompt)
        self.assertIn("绝不说“请稍等”", prompt)
        self.assertIn("立刻执行 Step 2", prompt)
        self.assertIn("模块切换必须同步界面", prompt)
        self.assertIn("当前产品只开放模块 1-6", prompt)
        self.assertIn("current_module 保持 6，不进入模块 7", prompt)
        self.assertNotIn("current_module = 7", prompt)

    def test_only_six_modules_are_open_in_both_web_views(self):
        for filename in ("index.html", "index_clean.html"):
            page = (HERMES / "templates" / filename).read_text(encoding="utf-8")
        self.assertIn("AVAILABLE_MODULE_COUNT", page)
        self.assertIn("尚未开发，敬请期待", page)
        self.assertIn("0/6", page)
        skills = (HERMES / "templates/skills.html").read_text(encoding="utf-8")
        videos = (HERMES / "templates/videos.html").read_text(encoding="utf-8")
        self.assertIn("s.m>6?'尚未开发，敬请期待'", skills)
        self.assertNotIn("fetch('/api/module8-video'", videos)
        self.assertIn("尚未开发，敬请期待", videos)

    def test_complete_original_route_set_is_present(self):
        routes = set()
        pattern = re.compile(r'(?:@app\.route|app\.add_url_rule)\(\s*["\']([^"\']+)')
        for path in HERMES.glob("*.py"):
            routes.update(pattern.findall(path.read_text(encoding="utf-8")))

        self.assertEqual(len(routes), 80)
        self.assertTrue(
            {
                "/api/chat",
                "/api/generate-report",
                "/api/generate-deliverable",
                "/api/generate-image",
                "/api/generate-video",
                "/api/foundation-report/generate",
                "/api/analyze-video",
                "/api/pipeline",
                "/api/replica",
                "/api/agnes/video",
                "/api/team-workbench/submit",
                "/api/ip12/productions/prepare",
                "/api/ip12/productions/quote",
                "/api/ip12/productions/confirm",
                "/api/ip12/productions/<production_id>",
                "/classic",
                "/skills",
                "/analytics",
                "/agnes-lab",
                "/team-workbench",
            }.issubset(routes)
        )

    def test_production_routes_keep_quote_tokens_server_side(self):
        source = (HERMES / "server.py").read_text(encoding="utf-8")
        for path in (
            "/api/ip12/productions/prepare",
            "/api/ip12/productions/quote",
            "/api/ip12/productions/confirm",
            "/api/ip12/productions/<production_id>",
        ):
            self.assertIn(path, source)
        self.assertIn('quote.pop("token", None)', source)
        self.assertIn('"idempotency_key": "ip12-" + production_id', source)
        self.assertIn('record.update(status="submitting"', source)
        self.assertIn('"/api/auth/internal/ip12/agent/action"', source)
        for field in ("account_id", "action", "input", "confirm", "quote_token", "idempotency_key"):
            self.assertIn(f'"{field}"', source)

    def test_main_view_exposes_three_featured_full_scripts(self):
        page = (HERMES / "templates/index.html").read_text(encoding="utf-8")
        self.assertIn("featured_3_v1", page)
        self.assertIn("3 篇精选口播文案", page)
        self.assertIn("latest.content", extract_js_function(page, "renderContentPack"))
        self.assertIn("isContentReviewMessage(turn.message)", extract_js_function(page, "sendTurn"))

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_inline_javascript_parses(self):
        failures = []
        for path in sorted((HERMES / "templates").glob("*.html")):
            html = path.read_text(encoding="utf-8")
            for index, script in enumerate(
                re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, re.I | re.S),
                1,
            ):
                if not script.strip():
                    continue
                with tempfile.NamedTemporaryFile(
                    "w", suffix=".js", encoding="utf-8", delete=False
                ) as handle:
                    handle.write(script)
                    temp_path = handle.name
                try:
                    result = subprocess.run(
                        ["node", "--check", temp_path], capture_output=True, text=True
                    )
                finally:
                    Path(temp_path).unlink(missing_ok=True)
                if result.returncode:
                    failures.append(f"{path.name} script {index}: {result.stderr}")
        self.assertEqual(failures, [])

    def test_runtime_files_use_environment_secrets(self):
        paths = list(HERMES.glob("*.py"))
        source = "\n".join(path.read_text(encoding="utf-8") for path in paths)
        self.assertNotRegex(source, r"sk-[A-Za-z0-9_-]{12,}")
        self.assertNotRegex(source, r"ark-[A-Za-z0-9_-]{12,}")
        literal_credentials = []
        for path in paths:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Assign):
                    names = [target.id for target in node.targets if isinstance(target, ast.Name)]
                    if (
                        names
                        and any(mark in names[0].upper() for mark in ("KEY", "TOKEN", "SECRET"))
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)
                        and node.value.value
                    ):
                        literal_credentials.append((path.name, node.lineno, names[0]))
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if (
                            isinstance(key, ast.Constant)
                            and str(key.value).lower() in {"authorization", "x-api-key"}
                            and isinstance(value, ast.Constant)
                            and value.value
                        ):
                            literal_credentials.append((path.name, node.lineno, key.value))
        self.assertEqual(literal_credentials, [])
        unit = (ROOT / "deploy/systemd/hermes-ip12-preview.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("EnvironmentFile=/home/ubuntu/.secrets/hermes-openai.env", unit)
        self.assertIn("port=3102", unit)

    def test_foundation_pdf_renderer_waits_for_chromium_exit(self):
        source = (HERMES / "server.py").read_text(encoding="utf-8")
        self.assertIn("subprocess.run(", source)
        self.assertIn("timeout=60", source)
        self.assertLess(
            source.index("playwright.chromium.executable_path"),
            source.index('shutil.which("chromium")'),
        )
        self.assertIn("价值主张诊断表", source)
        self.assertIn("故事库：只写有事实依据的故事", source)
        self.assertIn("不强制凑数量", source)
        self.assertNotIn("故事库（至少5个）", source)
        self.assertNotIn("列出5项客户要确认的项目", source)
        self.assertIn("内容资产使用表", source)
        self.assertIn("优化建议汇总", source)

    def test_foundation_pdf_review_is_embedded_with_structured_annotations(self):
        for filename in ("index.html", "index_clean.html"):
            source = (HERMES / "templates" / filename).read_text(encoding="utf-8")
            self.assertIn('id="foundationReviewer"', source)
            self.assertIn('id="foundationPdfFrame"', source)
            self.assertIn("URL.createObjectURL", source)
            self.assertIn("foundationPdfLoadId", source)
            self.assertIn("pdf-review-width", source)
            self.assertIn('role="separator"', source)
            self.assertIn("beginFoundationResize", source)
            self.assertIn("ip12-foundation-panel-width", source)
            self.assertIn("PDF 加载中…", source)
            self.assertNotIn("?preview=1#page=1&zoom=page-width", source)
            self.assertIn("function addFoundationAnnotation()", source)
            self.assertIn("function submitFoundationAnnotations()", source)
            self.assertIn("定位原文（可选）", source)
            self.assertIn("把批注交给 Agent", source)
            self.assertIn(
                "foundationEditing=false;closeFoundationReviewer();"
                "document.getElementById('userInput').placeholder='输入消息...'",
                source,
            )
            self.assertIn("@media", source)

    def test_service_security_boundary_is_registered(self):
        server = (HERMES / "server.py").read_text(encoding="utf-8")
        security = (HERMES / "security.py").read_text(encoding="utf-8")
        artifact_store = (HERMES / "artifact_store.py").read_text(encoding="utf-8")
        video_factory = (HERMES / "video_factory.py").read_text(encoding="utf-8")

        self.assertIn("register_security(app, DATA_DIR)", server)
        self.assertIn('HERMES_ENABLE_INTERNAL_TOOLS", "0"', server)
        self.assertIn('AUTH_BASE + "/api/auth/me"', security)
        self.assertIn('request.path == "/healthz"', security)
        self.assertIn("authentication service unavailable", security)
        self.assertIn("administrator permission required", security)
        self.assertIn("Hermes storage quota exceeded", security)
        self.assertIn("too many concurrent requests", security)
        self.assertIn("too many requests", security)
        self.assertIn('response.headers["X-Request-ID"]', security)
        self.assertIn('"duration_ms"', security)
        self.assertIn('"request_id"', security)
        self.assertIn("def atomic_write_bytes", artifact_store)
        self.assertIn("def atomic_append_bytes", artifact_store)
        self.assertIn("def video_work_dir", artifact_store)
        self.assertIn('LEGACY_ROLLBACK_DIRS = frozenset({"videos", "analyses", "uploads"})', artifact_store)
        self.assertIn("def _quota_paths():", artifact_store)
        self.assertIn('(?:ref_|replica_)?([0-9a-f]{10})', artifact_store)
        self.assertIn("owned_video_path(current_username(), filename)", video_factory)
        for filename in (
            "video_factory.py", "video_analyzer.py", "video_pipeline.py", "video_replica.py"
        ):
            source = (HERMES / filename).read_text(encoding="utf-8")
            self.assertIn("video_work_dir(", source, filename)
            self.assertIn("finalize_file(", source, filename)
        self.assertIn("if _is_metered(request.method)", security)
        media_library = (HERMES / "media_library.py").read_text(encoding="utf-8")
        video_analyzer = (HERMES / "video_analyzer.py").read_text(encoding="utf-8")
        self.assertIn("with storage_transaction():", media_library)
        self.assertIn('entry_id = f"{owner_id}_{new_asset_id()}"', media_library)

        runbook = (ROOT / "deploy" / "生产环境清单与还原手册.md").read_text(
            encoding="utf-8"
        )
        release_script = (ROOT / "deploy/hermes-ip12-release.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("http://127.0.0.1:3102/healthz", release_script)
        self.assertIn(
            "https://huangquechuanmei.com/workbench/ip12/healthz", release_script
        )
        self.assertIn("http://129.204.166.13:3101/healthz", release_script)
        self.assertNotIn("http://127.0.0.1:3102/ >/dev/null", release_script)
        self.assertEqual(
            video_analyzer.count('"--max-filesize", ANALYSIS_MAX_DOWNLOAD_ARG'), 2
        )
        self.assertIn("with reserve_capacity(ANALYSIS_MAX_DOWNLOAD_BYTES)", video_analyzer)
        agnes_routes = (HERMES / "agnes_routes.py").read_text(encoding="utf-8")
        team_routes = (HERMES / "team_workbench_routes.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("atomic_write_bytes", agnes_routes)
        self.assertIn("reserve_capacity", agnes_routes)
        self.assertIn("atomic_write_bytes", team_routes)
        self.assertIn("reserve_capacity", team_routes)

    def test_security_boundaries_and_runtime_ignores_are_kept(self):
        index = (HERMES / "templates/index.html").read_text(encoding="utf-8")
        classic = (HERMES / "templates/index_clean.html").read_text(encoding="utf-8")
        skills = (HERMES / "templates/skills.html").read_text(encoding="utf-8")
        team = (HERMES / "templates/team_workbench.html").read_text(encoding="utf-8")
        agnes = (HERMES / "templates/agnes_lab.html").read_text(encoding="utf-8")
        self.assertIn("marked.parse(eHtml(t))", index)
        self.assertIn("marked.parse(escHtml(text))", classic)
        self.assertIn("marked@15.0.12/lib/marked.umd.js", index)
        self.assertIn("marked@15.0.12/lib/marked.umd.js", classic)
        self.assertIn("typeof marked!=='undefined'", classic)
        self.assertIn("sanitizeMarked(marked.parse", index)
        self.assertIn("sanitizeMarked(marked.parse", classic)
        self.assertIn("escHtml(c.title)", classic)
        self.assertNotIn("<span>${c.title}", classic)
        self.assertIn("esc(d.report)", skills)
        self.assertIn("function safeUrl(s)", team)
        self.assertIn("const safeUrl=s=>", agnes)
        self.assertNotIn("/api/module8-video", (HERMES / "templates/videos.html").read_text(encoding="utf-8"))
        self.assertIn("span.textContent = msg", (HERMES / "templates/video_factory.html").read_text(encoding="utf-8"))

        requirements = (HERMES / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("yt-dlp", requirements)
        self.assertIn("pypdf", requirements)
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for path in (
            "server/hermes_ip12/data/",
            "server/hermes_ip12/media_library/",
            "server/hermes_ip12/knowledge/",
            "server/hermes_ip12/.agnes_key",
            "server/hermes_ip12/agnes_key.txt",
            "server/hermes_ip12/*cookies*.txt",
            "server/hermes_ip12/backups/",
            "server/hermes_ip12/nohup.out",
        ):
            self.assertIn(path, ignore)

        runbook = (ROOT / "deploy/生产环境清单与还原手册.md").read_text(
            encoding="utf-8"
        )
        self.assertIn('git archive "$HERMES_SHA"', runbook)
        self.assertIn("deploy/hermes-ip12-release.sh", runbook)
        self.assertIn("deploy/nginx-huangquechuanmei.conf", runbook)
        self.assertIn("hermes-last-backup", runbook)
        self.assertIn("deploy/hermes-ip12-release.sh", runbook)
        release_start = runbook.index("HERMES_STAGE=$(mktemp -d)")
        release_end = runbook.index("\n```", release_start)
        release = runbook[release_start:release_end]
        self.assertIn("scripts/migrate_hermes_artifacts.py", release)
        self.assertIn(
            'test -f "$HERMES_STAGE/scripts/migrate_hermes_artifacts.py"',
            release,
        )
        self.assertIn(
            'test -f "$HERMES_STAGE/deploy/hermes-ip12-release.sh"',
            release,
        )
        release_script = (ROOT / "deploy/hermes-ip12-release.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("trap rollback_release EXIT", release_script)
        self.assertIn("restore_file", release_script)
        self.assertIn("systemctl daemon-reload", release_script)
        self.assertIn("fail_if_requested rsync", release_script)
        self.assertIn("fail_if_requested pip", release_script)
        self.assertIn("fail_if_requested health", release_script)
        self.assertIn('DEPLOY_USER="${HERMES_DEPLOY_USER:-$(id -un)}"', release_script)
        self.assertIn('DEPLOY_GROUP="${HERMES_DEPLOY_GROUP:-$(id -gn)}"', release_script)
        self.assertIn(
            'install -d -o "$DEPLOY_USER" -g "$DEPLOY_GROUP" -m 0700',
            release_script,
        )
        self.assertIn("Hermes rollback FAILED; manual recovery required", release_script)
        self.assertIn('exit "$ROLLBACK_FAILURE_EXIT"', release_script)
        self.assertLess(
            release_script.index('systemctl stop "$SERVICE"'),
            release_script.index("--dry-run"),
        )
        self.assertLess(
            release_script.index("--dry-run"),
            release_script.index('"$HERMES_RELEASE_DIR/server/hermes_ip12/"'),
        )
        self.assertLess(
            release_script.index("--dry-run"),
            release_script.rindex('systemctl restart "$SERVICE"'),
        )
        env_example = (ROOT / "deploy" / "hermes-ip12.env.example").read_text(
            encoding="utf-8"
        )
        self.assertIn("HERMES_LEGACY_OWNER=", env_example)
        self.assertIn("HERMES_DATA_QUOTA_MB=2048", env_example)
        self.assertIn("HERMES_DEPLOY_USER=ubuntu", env_example)
        self.assertIn("HERMES_DEPLOY_GROUP=ubuntu", env_example)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_markdown_guard_blocks_script_protocols_and_falls_back(self):
        for filename in ("index.html", "index_clean.html"):
            source = (HERMES / "templates" / filename).read_text(encoding="utf-8")
            guard = extract_js_function(source, "isSafeMarkdownUrl")
            script = guard + r'''
if (isSafeMarkdownUrl("javascript:alert(1)")) process.exit(1);
if (isSafeMarkdownUrl("data:text/html,<script>alert(1)</script>")) process.exit(2);
if (!isSafeMarkdownUrl("/report/1")) process.exit(3);
if (!isSafeMarkdownUrl("https://example.com/report")) process.exit(4);
'''
            result = subprocess.run(
                ["node", "-e", script], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, filename + result.stderr)

        classic = (HERMES / "templates/index_clean.html").read_text(encoding="utf-8")
        fallback = "\n".join(
            extract_js_function(classic, name)
            for name in ("renderMarkdown", "escHtml")
        )
        script = r'''
global.document={createElement:()=>({value:"",set textContent(v){this.value=String(v||"")},get innerHTML(){return this.value.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")}})};
''' + fallback + r'''
const rendered=renderMarkdown("<img src=x onerror=alert(1)>\nnext");
if (!rendered.includes("&lt;img") || !rendered.includes("<br>")) process.exit(5);
'''
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_evidence_retry_discards_the_bad_draft_and_recovers_in_the_same_turn(self):
        import json
        import threading
        from types import SimpleNamespace

        source = (HERMES / "server.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        process_turn = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_process_model_turn"
        )
        module = ast.fix_missing_locations(ast.Module(body=[process_turn], type_ignores=[]))

        class HarnessError(Exception):
            pass

        class HarnessConflict(Exception):
            pass

        message = "我想先在一个垂直行业做出结果，再复制到其他行业"
        state = {
            "revision": 0,
            "completed_modules": [],
            "foundation_report": {},
            "pending": {"status": "editing", "draft": "不合格旧稿"},
        }
        model_calls = []
        persist_calls = []
        validation_error = ""
        persist_failures = 2

        def coach_model(snapshot, _message, repair_error=None):
            model_calls.append((repair_error, bool(snapshot["coach_state"].get("pending"))))
            return {
                "decision": "propose_checkpoint",
                "reply": "这是按已确认资料重新整理的结果。",
            }, message

        def persist_turn(
            _cid, user_message, _revision, raw, evidence, prefix="", discard_pending=False,
            message_id="",
        ):
            persist_calls.append((user_message, raw, evidence, prefix, discard_pending))
            if len(persist_calls) <= persist_failures:
                raise HarnessError(validation_error)
            return (prefix + "\n\n" if prefix else "") + raw["reply"], {"revision": 1}

        namespace = {
            "CONVERSATION_STATE_LOCK": threading.RLock(),
            "owned_conversation": lambda _cid: {"coach_state": state},
            "normalize_coach_state": lambda value: value,
            "_assert_expected_revision": lambda _state, _revision: None,
            "_persist_user_message": lambda _cid, _message, _revision, _request_id="": "test-message-id",
            "_model_snapshot_without_user": lambda convo, _message_id: convo,
            "coach_harness": SimpleNamespace(
                HarnessError=HarnessError,
                HarnessConflict=HarnessConflict,
                duration_conflict_decision=lambda _state, _message: None,
            ),
            "_coach_model_decision": coach_model,
            "_persist_model_turn": persist_turn,
            "_chat_result": lambda assistant, next_state: {
                "assistant": assistant,
                "state": next_state,
            },
            "app": SimpleNamespace(logger=SimpleNamespace(warning=lambda *_args: None)),
            "requests": SimpleNamespace(RequestException=RuntimeError),
            "json": json,
        }
        exec(compile(module, str(HERMES / "server.py"), "exec"), namespace)
        cases = (
            "模型档案更新缺少可回查的用户原话",
            "确认稿包含未经证实的身份、经历或结果用语“专家”",
            "选题中的“医疗”没有出现在它绑定的用户原话里",
        )
        for validation_error in cases:
            with self.subTest(validation_error=validation_error):
                model_calls.clear()
                persist_calls.clear()
                result, status = namespace["_process_model_turn"]("cid", message)

                self.assertEqual(status, 200)
                self.assertEqual([item[1] for item in model_calls], [True, True, False])
                self.assertIsNone(model_calls[0][0])
                self.assertEqual(model_calls[1][0], validation_error)
                self.assertIn(validation_error, model_calls[2][0])
                self.assertEqual(len(persist_calls), 3)
                user_message, recovered, evidence, recovery_prefix, discard_pending = persist_calls[-1]
                self.assertEqual(user_message, message)
                self.assertEqual(evidence, message)
                self.assertEqual(recovered["decision"], "propose_checkpoint")
                self.assertTrue(discard_pending)
                self.assertIn("错在", recovery_prefix)
                self.assertIn("重新整理的结果", result["assistant"])
                self.assertNotIn("请回复“", result["assistant"])

        validation_error = "选题中的“成功”没有出现在它绑定的用户原话里"
        persist_failures = 3
        model_calls.clear()
        persist_calls.clear()
        result, status = namespace["_process_model_turn"]("cid", message)

        self.assertEqual(status, 200)
        self.assertEqual(len(model_calls), 3)
        self.assertEqual(len(persist_calls), 4)
        self.assertEqual(persist_calls[-1][1]["decision"], "answer_only")
        self.assertTrue(persist_calls[-1][-1])
        self.assertIn("未确认草稿已清除", result["assistant"])
        self.assertNotIn("请回复“", result["assistant"])

    def test_module_five_step_two_uses_the_specialized_topic_compiler(self):
        source = (HERMES / "server.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        coach_turn = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_coach_model_decision"
        )
        module = ast.fix_missing_locations(ast.Module(body=[coach_turn], type_ignores=[]))
        state = {
            "current_module": 5,
            "module_step": 1,
            "completed_modules": [1, 2, 3, 4],
            "ip_profile": {},
        }
        calls = []

        def module_five(convo, message, repair_error=""):
            calls.append((convo, message, repair_error))
            return {"decision": "propose_checkpoint"}, "evidence"

        namespace = {
            "normalize_coach_state": lambda value: value,
            "_intake_pending": lambda _state: False,
            "_coach_module_five_topics": module_five,
        }
        exec(compile(module, str(HERMES / "server.py"), "exec"), namespace)
        convo = {"coach_state": state}
        result = namespace["_coach_model_decision"](
            convo,
            "继续",
            repair_error="选题中的‘成功’没有依据",
        )

        self.assertEqual(result, ({"decision": "propose_checkpoint"}, "evidence"))
        self.assertEqual(calls, [(convo, "继续", "选题中的‘成功’没有依据")])


@unittest.skipUnless(
    importlib.util.find_spec("flask") and importlib.util.find_spec("requests"),
    "Hermes route dependencies are not installed",
)
class HermesIP12ProductionRuntimeTests(unittest.TestCase):
    def test_typed_production_state_quote_confirm_and_canvas_recovery(self):
        script = r'''
import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import server
import security

sys.path.insert(0, str(Path(server.__file__).resolve().parents[1]))
import hq_cli_api

server.current_account_id = lambda: "acct_a"
security._validate_token = lambda token: {
    "admin-token": {"account_id": "acct_a", "username": "admin", "role": "admin"},
}.get(token)
security.RATE_REQUESTS = 1000
client = server.app.test_client()
client.environ_base["HTTP_AUTHORIZATION"] = "Bearer admin-token"

cid = "typedproduction01"
state = server.initial_coach_state()
server.save_conversation(cid, {
    "id": cid,
    "title": "typed production",
    "messages": [{"role": "user", "content": "请把这篇正文做成多媒体成品，保留我的原话。"}],
    "coach_state": state,
    "reports": {},
    "owner_account_id": "acct_a",
    "deliverables": {"6": {"kind": "content_pack_v1", "categories": [{
        "id": "category_1",
        "name": "内容种类",
        "topics": [{
            "id": "topic_1",
            "title": "精选选题",
            "status": "ready",
            "versions": [{"version": 1, "content": "这是用户确认过、可直接进入生产的完整口播正文。"}],
        }],
    }]}},
})
revision = state["revision"]
target = {"category_id": "category_1", "topic_id": "topic_1"}
intent_response = client.post("/api/chat-complete", json={
    "conversation_id": cid,
    "message": "用 Grok 把这篇做成 9:16 竖屏视频",
    "content_target": target,
    "expected_revision": revision,
    "request_id": "prepare-video-from-chat",
})
assert intent_response.status_code == 200, intent_response.get_data(as_text=True)
intent_body = intent_response.get_json()
assert intent_body["actions"][0]["type"] == "prepare_production", intent_body
assert intent_body["actions"][0]["requested_result"] == "video", intent_body
assert intent_body["actions"][0]["preferred_action"] == "video-generate", intent_body
assert intent_body["actions"][0]["options"] == {"ratio": "9:16"}, intent_body
assert not server.load_conversation(cid).get("productions"), intent_body
revision = intent_body["state"]["revision"]
original_messages = json.loads(json.dumps(server.load_conversation(cid)["messages"], ensure_ascii=False))

def prepare(family, options_marker=None, preferred_action=None):
    body = {
        "conversation_id": cid,
        "content_target": target,
        "expected_revision": revision,
        "requested_result": family,
    }
    if options_marker is not None:
        body["options"] = options_marker
    if preferred_action is not None:
        body["preferred_action"] = preferred_action
    response = client.post("/api/ip12/productions/prepare", json=body)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()

# Missing and typed fields are returned before any execution call.  Filling the
# same production later is allowed and changes its input digest.
missing_image = prepare("image")
assert missing_image["status"] == "blocked_prerequisite", missing_image
assert missing_image["missing"] == ["prompt"], missing_image
assert missing_image["schema"]["properties"]["prompt"]["type"] == "string"
image_id = missing_image["production_id"]
empty_digest = server.load_conversation(cid)["productions"][image_id]["input_digest"]

bad_image = prepare("image", {"prompt": 7})
assert bad_image["status"] == "blocked_prerequisite", bad_image
assert bad_image["missing"] == [], bad_image
assert "类型" in bad_image["validation_error"], bad_image

audio = prepare("audio", {"text": "请用自然语气读出这段旁白。"})
video = prepare("video", {"avatar_id": 7, "voice": "voice-demo"})
canvas = prepare("canvas", {"board_id": "board_canvas_1", "base_version": 1, "prompt": "把这篇口播放进画布。"})
generic_video = prepare("video", {"prompt": "把正文改编成海边日出短片。"}, "video-generate")
for prepared, required in (
    (audio, "text"), (video, "avatar_id"), (canvas, "prompt"),
):
    assert prepared["status"] == "draft", prepared
    assert required in prepared["schema"]["required"], prepared
assert canvas["recommended_action"] == "canvas-ops", canvas
assert generic_video["recommended_action"] == "video-generate", generic_video
assert generic_video["status"] == "draft" and "prompt" in generic_video["schema"]["required"]
audio_record = server.load_conversation(cid)["productions"][audio["production_id"]]
assert server._production_input(audio_record, {"text": "第一段。\n\n第二段。\t继续。"})["text"] == "第一段。 第二段。 继续。"

# The HTTP helper uses the unified internal action contract and never places
# account_id inside the action input.
bridge_response = Mock(status_code=200)
bridge_response.json.return_value = {"job_id": 1, "status": "queued"}
with patch.object(server, "INTERNAL_ACTION_TOKEN", "internal-test-token"), \
     patch.object(server.requests, "post", return_value=bridge_response) as bridge_post:
    server._bridge_action(
        "acct_a", "image-generate", {"prompt": "海边日出"}, confirm=True,
        quote_token="signed-quote", idempotency_key="ip12-confirm-0001",
    )
sent = bridge_post.call_args
assert sent.args[0] == server.AUTH_BASE + "/api/auth/internal/ip12/agent/action", sent
assert sent.kwargs["headers"] == {"X-HQ-Internal-Token": "internal-test-token"}
assert sent.kwargs["json"] == {
    "account_id": "acct_a",
    "action": "image-generate",
    "input": {"prompt": "海边日出"},
    "confirm": True,
    "quote_token": "signed-quote",
    "idempotency_key": "ip12-confirm-0001",
}

quote_calls = []
def image_bridge(account_id, action, input_body, **kwargs):
    quote_calls.append((account_id, action, input_body, kwargs))
    if kwargs.get("confirm"):
        return {"job_id": "101", "status": "queued"}
    if action == "task":
        return {"id": 101, "kind": "image", "status": "done", "result": {
            "type": "image", "file": "generated.png", "url": "https://cdn.example/generated.png",
            "files": ["generated.png"], "urls": ["https://cdn.example/generated.png"],
        }}
    return {"quote_token": "private-image-quote", "cost": 4, "points": 4, "expires_in": 300}

with patch.object(server, "_bridge_action", side_effect=image_bridge):
    quoted = client.post("/api/ip12/productions/quote", json={
        "conversation_id": cid,
        "production_id": image_id,
        "expected_revision": revision,
        "options": {"prompt": "第一版海报"},
    })
assert quoted.status_code == 200, quoted.get_data(as_text=True)
quoted_body = quoted.get_json()
assert quoted_body["billing"] == "paid" and quoted_body["cost"] == 4, quoted_body
filled = server.load_conversation(cid)["productions"][image_id]
assert filled["options"] == {"prompt": "第一版海报"}
assert filled["input_digest"] != empty_digest
assert quote_calls[0][3]["idempotency_key"] == filled["idempotency_key"]

# Options supplied at confirm are part of the quoted digest.  A changed option
# invalidates the old quote without submitting anything.
with patch.object(server, "_bridge_action", side_effect=image_bridge):
    changed = client.post("/api/ip12/productions/confirm", json={
        "conversation_id": cid,
        "production_id": image_id,
        "expected_revision": revision,
        "confirmation_id": "confirm-image-001",
        "options": {"prompt": "第二版海报"},
    })
assert changed.status_code == 409, changed.get_data(as_text=True)
assert changed.get_json()["code"] == "input_changed"
assert not [call for call in quote_calls if call[3].get("confirm")]

# Requote the changed input, submit once, and make a double-click replay read
# the existing job instead of opening another paid task.
with patch.object(server, "_bridge_action", side_effect=image_bridge):
    requoted = client.post("/api/ip12/productions/quote", json={
        "conversation_id": cid,
        "production_id": image_id,
        "expected_revision": revision,
    })
    assert requoted.status_code == 200, requoted.get_data(as_text=True)
    confirmed = client.post("/api/ip12/productions/confirm", json={
        "conversation_id": cid,
        "production_id": image_id,
        "expected_revision": revision,
        "confirmation_id": "confirm-image-001",
    })
    replayed = client.post("/api/ip12/productions/confirm", json={
        "conversation_id": cid,
        "production_id": image_id,
        "expected_revision": revision,
        "confirmation_id": "confirm-image-001",
    })
    restored = client.get(f"/api/ip12/productions/{image_id}?conversation_id={cid}")
assert confirmed.status_code == 200 and confirmed.get_json()["production"]["job_id"] == "101"
assert replayed.status_code == 200 and replayed.get_json()["replayed"] is True
assert restored.get_json()["production"]["status"] == "done"
assert restored.get_json()["production"]["asset_refs"][0]["url"] == "https://cdn.example/generated.png"
assert restored.get_json()["production"]["last_error_code"] == ""
assert len([call for call in quote_calls if call[3].get("confirm")]) == 1, quote_calls
assert all(call[3]["idempotency_key"] == filled["idempotency_key"] for call in quote_calls), quote_calls

for action, kind, url_field, file_field in (
    ("audio-generate", "audio", "audio_url", "audio_file"),
    ("video-generate", "video", "video_url", "video_file"),
):
    nested_record = {"action": action, "capability_family": kind, "asset_refs": [], "last_error_code": "result_link_pending"}
    server._set_production_result(nested_record, {
        "status": "done", "kind": kind,
        "result": {url_field: "https://cdn.example/output", file_field: "output.bin"},
    })
    assert nested_record["asset_refs"] == [{
        "kind": kind, "url": "https://cdn.example/output", "name": "output.bin", "file": "output.bin",
    }]
    assert nested_record["last_error_code"] == ""
mixed_video = {"action": "video-generate", "capability_family": "video", "asset_refs": []}
server._set_production_result(mixed_video, {
    "status": "done", "kind": "xiaole_video", "result": {
        "type": "video", "image_url": "https://cdn.example/cover.jpg", "image_file": "cover.jpg",
        "video_url": "https://cdn.example/output.mp4", "video_file": "output.mp4",
    },
})
assert mixed_video["asset_refs"] == [{
    "kind": "video", "url": "https://cdn.example/output.mp4",
    "name": "output.mp4", "file": "output.mp4",
}]
finished_change = client.post("/api/ip12/productions/confirm", json={
    "conversation_id": cid,
    "production_id": image_id,
    "expected_revision": revision,
    "confirmation_id": "confirm-image-001",
    "options": {"prompt": "不能覆盖已完成记录的第三版海报"},
})
assert finished_change.status_code == 409
assert finished_change.get_json()["code"] == "production_already_submitted"
assert server.load_conversation(cid)["productions"][image_id]["options"] == {"prompt": "第二版海报"}

# Failed work and a completed refund remain visible after refresh.
audio_id = audio["production_id"]
def audio_bridge(account_id, action, input_body, **kwargs):
    if action == "task":
        return {"job_id": "202", "status": "failed", "code": "provider_failed", "refund_status": "refunded"}
    if kwargs.get("confirm"):
        return {"job_id": "202", "status": "queued"}
    return {"quote_token": "private-audio-quote", "cost": 2, "points": 2, "expires_in": 300}
with patch.object(server, "_bridge_action", side_effect=audio_bridge):
    assert client.post("/api/ip12/productions/quote", json={
        "conversation_id": cid, "production_id": audio_id, "expected_revision": revision,
    }).status_code == 200
    assert client.post("/api/ip12/productions/confirm", json={
        "conversation_id": cid, "production_id": audio_id, "expected_revision": revision,
        "confirmation_id": "confirm-audio-001",
    }).status_code == 200
    failed = client.get(f"/api/ip12/productions/{audio_id}?conversation_id={cid}")
failed_record = failed.get_json()["production"]
assert failed_record["status"] == "failed" and failed_record["refund_status"] == "refunded", failed_record
assert failed_record["last_error_code"] == "provider_failed"

# A successful submit response without a durable result reference stays in
# submitting and is recovered with the same idempotency key.
video_id = video["production_id"]
video_calls = []
def video_bridge(account_id, action, input_body, **kwargs):
    if action == "task":
        video_calls.append((action, input_body, kwargs["idempotency_key"]))
        return {"job_id": "303", "status": "done", "asset_refs": [{"id": "asset-video-1", "kind": "video"}]}
    if not kwargs.get("confirm"):
        return {"quote_token": "private-video-quote", "cost": 8, "points": 8, "expires_in": 300}
    video_calls.append((action, input_body, kwargs["idempotency_key"]))
    return {"job_id": "303", "status": "done"}
with patch.object(server, "_bridge_action", side_effect=video_bridge):
    assert client.post("/api/ip12/productions/quote", json={
        "conversation_id": cid, "production_id": video_id, "expected_revision": revision,
    }).status_code == 200
    unlinked = client.post("/api/ip12/productions/confirm", json={
        "conversation_id": cid, "production_id": video_id, "expected_revision": revision,
        "confirmation_id": "confirm-video-001",
    })
    recovered_video = client.get(f"/api/ip12/productions/{video_id}?conversation_id={cid}")
assert unlinked.status_code == 200, unlinked.get_data(as_text=True)
assert unlinked.get_json()["production"]["last_error_code"] == "result_link_pending"
assert recovered_video.get_json()["production"]["job_id"] == "303"
assert recovered_video.get_json()["production"]["status"] == "done"
assert recovered_video.get_json()["production"]["asset_refs"][0]["id"] == "asset-video-1"
assert [call[0] for call in video_calls] == ["digital-ip-text-generate", "task"], video_calls
assert video_calls[0][2] == video_calls[1][2], video_calls

# Canvas quote is free, while confirm uses the real canvas-ops input shape.
canvas_id = canvas["production_id"]
canvas_record = server.load_conversation(cid)["productions"][canvas_id]
canvas_input = server._production_input(canvas_record, canvas_record["options"])
canvas_plan = hq_cli_api.action_plan("canvas-ops", canvas_input)
assert canvas_plan["kind"] == "canvas-ops"
assert canvas_plan["board_id"] == "board_canvas_1"
assert canvas_plan["payload"]["op_id"].startswith("hqcli-")
canvas_calls = []
def canvas_bridge(account_id, action, input_body, **kwargs):
    canvas_calls.append((account_id, action, input_body, kwargs))
    assert kwargs.get("confirm") is True
    return {"version": 2, "batch": {"op_id": input_body["op_id"]}, "board": {"id": input_body["board_id"]}}
with patch.object(server, "_bridge_action", side_effect=canvas_bridge):
    canvas_quote = client.post("/api/ip12/productions/quote", json={
        "conversation_id": cid, "production_id": canvas_id, "expected_revision": revision,
    })
    assert canvas_calls == [], canvas_calls
    canvas_done = client.post("/api/ip12/productions/confirm", json={
        "conversation_id": cid, "production_id": canvas_id, "expected_revision": revision,
        "confirmation_id": "confirm-canvas-001",
    })
    canvas_replay = client.post("/api/ip12/productions/confirm", json={
        "conversation_id": cid, "production_id": canvas_id, "expected_revision": revision,
        "confirmation_id": "confirm-canvas-001",
    })
assert canvas_quote.get_json()["billing"] == "free" and canvas_quote.get_json()["cost"] == 0
assert canvas_done.get_json()["production"]["status"] == "done"
assert canvas_done.get_json()["production"]["canvas_ref"] == {"board_id": "board_canvas_1", "version": 2}
assert canvas_replay.get_json()["replayed"] is True
assert len(canvas_calls) == 1, canvas_calls
assert canvas_calls[0][1] == "canvas-ops"
assert canvas_calls[0][3]["quote_token"] == ""

# A lost Canvas response is retried by GET with the exact same op_id and
# production idempotency key, so auth_server can return its saved canvas batch.
recovery = prepare("canvas", {"board_id": "board_canvas_2", "base_version": 1, "prompt": "写入恢复节点。"})
recovery_id = recovery["production_id"]
recovery_calls = []
def recovery_bridge(account_id, action, input_body, **kwargs):
    recovery_calls.append((json.loads(json.dumps(input_body)), kwargs["idempotency_key"]))
    if len(recovery_calls) == 1:
        raise RuntimeError("response lost")
    return {"version": 2, "batch": {"op_id": input_body["op_id"]}, "board": {"id": input_body["board_id"]}}
with patch.object(server, "_bridge_action", side_effect=recovery_bridge):
    recovery_quote = client.post("/api/ip12/productions/quote", json={
        "conversation_id": cid, "production_id": recovery_id, "expected_revision": revision,
    })
    pending = client.post("/api/ip12/productions/confirm", json={
        "conversation_id": cid, "production_id": recovery_id, "expected_revision": revision,
        "confirmation_id": "confirm-canvas-002",
    })
    recovered_canvas = client.get(f"/api/ip12/productions/{recovery_id}?conversation_id={cid}")
assert recovery_quote.status_code == 200 and pending.status_code == 202
assert recovered_canvas.get_json()["production"]["status"] == "done"
assert len(recovery_calls) == 2 and recovery_calls[0] == recovery_calls[1], recovery_calls
assert hq_cli_api.action_plan("canvas-ops", recovery_calls[0][0])["kind"] == "canvas-ops"

# A quoted production is tied to the selected script version, even if the next
# version happens to reuse similar wording.
stale = prepare("video", {"avatar_id": 9, "voice": "voice-demo"})
stale_id = stale["production_id"]
with patch.object(server, "_bridge_action", return_value={
    "quote_token": "private-stale-quote", "cost": 8, "points": 8, "expires_in": 300,
}):
    assert client.post("/api/ip12/productions/quote", json={
        "conversation_id": cid, "production_id": stale_id, "expected_revision": revision,
    }).status_code == 200
project = server.load_conversation(cid)
project["deliverables"]["6"]["categories"][0]["topics"][0]["versions"].append({
    "version": 2, "content": "这是用户确认过、可直接进入生产的第二版完整口播正文。",
})
server.save_conversation(cid, project)
stale_response = client.get(f"/api/ip12/productions/{stale_id}?conversation_id={cid}")
assert stale_response.get_json()["production"]["status"] == "stale"
assert stale_response.get_json()["production"]["last_error_code"] == "source_changed"

# All four families survive a project refresh, private quote tokens stay on the
# server, accounts remain isolated, and production never mutates chat messages.
public_project = client.get(f"/api/conversations/{cid}").get_json()
families = {item["capability_family"] for item in public_project["productions"]}
assert {"image", "audio", "video", "canvas"}.issubset(families), families
assert "private-" not in json.dumps(public_project, ensure_ascii=False)
assert server.load_conversation(cid)["messages"] == original_messages
server.current_account_id = lambda: "acct_b"
assert client.get(f"/api/conversations/{cid}").status_code == 404
assert client.get(f"/api/ip12/productions/{image_id}?conversation_id={cid}").status_code == 404
print("IP12_PRODUCTION_RUNTIME_OK")
'''
        with tempfile.TemporaryDirectory() as data_dir:
            env = os.environ.copy()
            env.update(
                OPENAI_API_KEY="dummy",
                HERMES_HOME=data_dir,
                HERMES_DATA_DIR=data_dir,
                HQ_INTERNAL_TOKEN="internal-test-token",
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=HERMES,
                env=env,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("IP12_PRODUCTION_RUNTIME_OK", result.stdout)


@unittest.skipUnless(
    importlib.util.find_spec("flask") and importlib.util.find_spec("requests"),
    "Hermes runtime dependencies are not installed",
)
class HermesIP12ProjectRuntimeTests(unittest.TestCase):
    def test_project_limit_receipt_recovery_and_first_artifact_notice(self):
        script = r'''
import server
import security
import threading

server.current_account_id = lambda: "acct_limit"
server.MAX_PROJECTS_PER_ACCOUNT = 2
security._validate_token = lambda token: {"account_id": "acct_limit", "username": "limit", "role": "member"}
security.RATE_REQUESTS = 100
client = server.app.test_client()
client.environ_base["HTTP_AUTHORIZATION"] = "Bearer test-token"

one = client.post("/api/conversations", json={"title": "Project 1"})
two = client.post("/api/conversations", json={"title": "Project 2"})
assert one.status_code == 200 and two.status_code == 200
three = client.post("/api/conversations", json={"title": "Project 3"})
assert three.status_code == 409
assert three.get_json() == {
    "ok": False, "code": "ip12_project_limit", "error": "最多允许创建两个 Project",
}
assert client.delete(f"/api/conversations/{two.get_json()['id']}").status_code == 200
replacement = client.post("/api/conversations", json={"title": "Replacement"})
assert replacement.status_code == 200
replacement_id = replacement.get_json()["id"]
stale_replacement = server.load_conversation(replacement_id)
assert client.delete(f"/api/conversations/{replacement_id}").status_code == 200
assert server.save_conversation(replacement_id, stale_replacement) is False
assert not server.conversation_path(replacement_id).exists()
assert client.post("/api/conversations", json={"title": "Replacement 2"}).status_code == 200

cid = one.get_json()["id"]
convo = server.load_conversation(cid)
convo["turn_receipts"] = [{
    "request_id": "recover_turn_1",
    "result": {"ok": True, "assistant": "已恢复", "state": convo["coach_state"]},
}]
server.save_conversation(cid, convo)
pending = client.get(f"/api/conversations/{cid}?receipt=still_processing")
assert pending.status_code == 202
assert pending.get_json() == {"ok": True, "status": "processing"}
recovered = client.get(f"/api/conversations/{cid}?receipt=recover_turn_1")
assert recovered.status_code == 200
assert recovered.get_json()["replayed"] is True
assert recovered.get_json()["assistant"] == "已恢复"
assert client.get(f"/api/conversations/{cid}?receipt=bad%20receipt").status_code == 400

calls = []
entered = threading.Event()
release = threading.Event()
original_model_turn = server._process_model_turn
def slow_model_turn(*args, **kwargs):
    calls.append(args[1])
    entered.set()
    assert release.wait(2)
    state = server.load_conversation(cid)["coach_state"]
    return server._chat_result("并发完成", state), 200
server._process_model_turn = slow_model_turn
body = {
    "conversation_id": cid,
    "message": "只处理一次",
    "expected_revision": server.load_conversation(cid)["coach_state"]["revision"],
    "request_id": "concurrent_turn_1",
}
first_result = []
worker = threading.Thread(target=lambda: first_result.append(server.process_chat_request(body)))
worker.start()
assert entered.wait(2)
duplicate = server.process_chat_request(body)
assert duplicate[1] == 202 and duplicate[0]["status"] == "processing"
blocked_delete = client.delete(f"/api/conversations/{cid}")
assert blocked_delete.status_code == 409
assert blocked_delete.get_json()["error"] == "请等待当前回复完成后再删除 Project"
release.set()
worker.join(2)
assert not worker.is_alive()
assert len(calls) == 1 and first_result[0][1] == 200
assert server.process_chat_request(body)[0]["replayed"] is True
server._process_model_turn = original_model_turn

server.generate_module_report = lambda convo_id, module_id: "报告正文"
first_report = client.post("/api/generate-report", json={"conversation_id": cid, "module": 1})
second_report = client.post("/api/generate-report", json={"conversation_id": cid, "module": 1})
assert first_report.status_code == 200 and second_report.status_code == 200
assert first_report.get_json()["artifact_notice"] == server.FIRST_ARTIFACT_NOTICE
assert second_report.get_json()["artifact_notice"] == ""
saved = server.load_conversation(cid)
assert saved["artifact_notice_sent"] is True
assert saved["artifact_notice_module"] == 1
assert [item["content"] for item in saved["messages"]].count(server.FIRST_ARTIFACT_NOTICE) == 1
assert b"IP Project" in client.get("/classic").data
print("IP12_PROJECT_RUNTIME_OK")
'''
        with tempfile.TemporaryDirectory() as data_dir:
            env = os.environ.copy()
            env.update(OPENAI_API_KEY="dummy", HERMES_HOME=data_dir, HERMES_DATA_DIR=data_dir)
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=HERMES, env=env,
                capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("IP12_PROJECT_RUNTIME_OK", result.stdout)


@unittest.skipUnless(
    importlib.util.find_spec("flask") and importlib.util.find_spec("requests") and importlib.util.find_spec("pypdf"),
    "Hermes runtime dependencies are not installed",
)
class HermesIP12RuntimeTests(unittest.TestCase):
    def test_app_registers_and_core_storage_round_trip_works(self):
        script = r'''
import io
import base64
import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import server
from server import _foundation_generation_active, _foundation_html, _foundation_source_messages, _render_foundation_pdf, _validate_foundation_pdf, app, parse_coach_state_updates
import security
import artifact_store
import image_services
import media_library
import video_analyzer
import video_factory
import video_pipeline
import video_replica
import video_vision

server.current_account_id = lambda: "acct_a"
server.MAX_PROJECTS_PER_ACCOUNT = 1000
security._validate_token = lambda token: {
    "admin-token": {"account_id": "acct_a", "username": "admin", "role": "admin"},
    "member-a-token": {"account_id": "acct_a", "username": "member-a", "role": "member"},
    "member-b-token": {"account_id": "acct_b", "username": "member-b", "role": "member"},
}.get(token)
security.RATE_REQUESTS = 1000
routes = {rule.rule for rule in app.url_map.iter_rules() if rule.endpoint != "static"}
assert len(routes) == 80, len(routes)
assert all(
    security._is_metered(method)
    for rule in app.url_map.iter_rules()
    for method in rule.methods
    if method in {"POST", "PUT", "PATCH", "DELETE"}
)
with patch.object(server, "MODEL", "deepseek-v4-flash"), patch.object(server.requests, "post") as request_model:
    invalid_json = Mock(status_code=200)
    invalid_json.json.return_value = {"choices": [{"message": {"content": '{"decision":'}}]}
    valid_json = Mock(status_code=200)
    valid_json.json.return_value = {"choices": [{"message": {"content": '{"decision":"answer_only"}'}}]}
    request_model.side_effect = [invalid_json, valid_json]
    response = server.call_ai(
        [{"role": "system", "content": "只输出 JSON"}],
        response_format={
            "type": "json_schema",
            "json_schema": {"schema": {"type": "object", "required": ["decision"]}},
        },
    )
    assert response is valid_json
    assert request_model.call_count == 2
    deepseek_payload = request_model.call_args.kwargs["json"]
    assert deepseek_payload["response_format"] == {"type": "json_object"}
    assert deepseek_payload["thinking"] == {"type": "disabled"}
    assert '"required":["decision"]' in deepseek_payload["messages"][0]["content"]
    assert deepseek_payload["messages"][-2] == {"role": "assistant", "content": '{"decision":'}
    assert "上一次输出不是完整 JSON" in deepseek_payload["messages"][-1]["content"]

with patch.object(server, "MODEL", "gpt-5.6-luna"), patch.object(server.requests, "post") as request_model:
    completed = Mock(status_code=200)
    request_model.return_value = completed
    response = server.call_ai([{"role": "user", "content": "你好"}], max_tokens=1200)
    assert response is completed
    luna_payload = request_model.call_args.kwargs["json"]
    assert luna_payload["max_completion_tokens"] == 1200
    assert "max_tokens" not in luna_payload
    assert "temperature" not in luna_payload

transitioned = parse_coach_state_updates(
    "好，我们进入模块2：人设塑造。",
    {"current_module": 1, "completed_modules": [], "module_step": 0},
)
assert transitioned["current_module"] == 1, transitioned
assert transitioned["completed_modules"] == [], transitioned
foundation = parse_coach_state_updates(
    "✅ 模块 4 完成",
    {"current_module": 4, "completed_modules": [1, 2, 3], "module_step": 0},
)
assert foundation["current_module"] == 4, foundation
assert foundation["completed_modules"] == [1, 2, 3], foundation
assert "foundation_report" not in foundation, foundation
blocked_transition = parse_coach_state_updates(
    "✅ 模块 4 完成。接下来进入模块 5。",
    {"current_module": 4, "completed_modules": [1, 2, 3], "module_step": 0},
)
assert blocked_transition["current_module"] == 4, blocked_transition
assert blocked_transition["completed_modules"] == [1, 2, 3], blocked_transition
revisited = parse_coach_state_updates(
    "✅ 模块 4 完成。接下来进入模块 5。",
    {"current_module": 4, "completed_modules": [1, 2, 3, 4], "module_step": 0,
     "foundation_report": {"status": "confirmed"}},
)
assert revisited["current_module"] == 5, revisited
assert revisited["foundation_report"]["status"] == "confirmed", revisited
finished = parse_coach_state_updates(
    "✅ 模块 6 完成。接下来进入模块 7。",
    {"current_module": 6, "completed_modules": [1, 2, 3, 4, 5], "module_step": 0,
     "foundation_report": {"status": "confirmed"}},
)
assert finished["current_module"] == 6, finished
assert finished["completed_modules"] == [1, 2, 3, 4, 5], finished
legacy = parse_coach_state_updates(
    "继续复盘",
    {"current_module": 8, "completed_modules": list(range(1, 8)), "module_step": 2,
     "foundation_report": {"status": "confirmed"}},
)
assert legacy["current_module"] == 6, legacy
assert legacy["completed_modules"] == [1, 2, 3, 4, 5, 6], legacy
assert 5 not in parse_coach_state_updates(
    "✅ 模块 5 完成",
    {"current_module": 4, "completed_modules": [1, 2, 3, 4], "module_step": 0,
     "foundation_report": {"status": "awaiting_confirmation"}},
)["completed_modules"]
assert parse_coach_state_updates(
    "本次诊断全部完成，正式结业",
    {"current_module": 1, "completed_modules": [], "module_step": 0},
)["completed_modules"] == []
source_messages = [
    {"role": "user", "content": "模块一资料"},
    {"role": "assistant", "content": "继续模块四"},
    {"role": "assistant", "content": "✅ 模块 4 完成"},
    {"role": "user", "content": "模块五资料"},
]
assert _foundation_source_messages({"messages": source_messages}) == source_messages[:3]
transition_messages = source_messages[:2] + [
    {"role": "assistant", "content": "接下来进入模块 5"},
    {"role": "user", "content": "模块五资料"},
]
assert _foundation_source_messages({"messages": transition_messages}) == transition_messages[:2]
assert _foundation_source_messages({
    "messages": source_messages,
    "coach_state": {"foundation_source_message_count": 2},
}) == source_messages[:2]
assert not _foundation_generation_active({"status": "generating", "started_at": "2099-01-01 00:00:00", "process_run_id": "old-process"})

def write_test_pdf(path, pages=8):
    stream = b"q\nQ\n%" + b"0" * 10000 + b"\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + b" ".join(f"{i} 0 R".encode() for i in range(3, 3 + pages)) + f"] /Count {pages} >>".encode(),
    ]
    objects.extend(
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents {3 + pages} 0 R >>".encode()
        for _ in range(pages)
    )
    objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"endstream")
    data = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data)); data.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(data); data.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(data)

valid_pdf = Path(os.environ["HERMES_DATA_DIR"]) / "valid.pdf"
write_test_pdf(valid_pdf)
assert _validate_foundation_pdf(valid_pdf) == 8
if shutil.which("pdfinfo"):
    assert subprocess.run(["pdfinfo", str(valid_pdf)], capture_output=True).returncode == 0
invalid_pdf = Path(os.environ["HERMES_DATA_DIR"]) / "invalid.pdf"
invalid_body = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n" + b"/Type /Page\n" * 8 + b"0" * 10000
invalid_pdf.write_bytes(invalid_body + b"\nxref\nthis is not a cross-reference table\ntrailer\n<< /Root 1 0 R /Size 10 >>\nstartxref\n" + str(len(invalid_body) + 1).encode() + b"\n%%EOF\n")
try:
    _validate_foundation_pdf(invalid_pdf)
    raise AssertionError("structurally invalid PDF was accepted")
except RuntimeError:
    pass
report_html = _foundation_html("""# 忽略的总标题
## 模块一｜定位诊断
### 核心关键词
#### 故事名称：从无到有
1. **实战**：有可验证经历。
| 场景 | 建议口径 |
| --- | --- |
| 账号封面 | 直接说结果 |
> 待本人确认
""")
assert "模块一｜定位诊断" in report_html
assert "<table>" in report_html and "账号封面" in report_html
assert "<blockquote>待本人确认</blockquote>" in report_html
assert "<h4>故事名称：从无到有</h4>" in report_html

render_root = Path(os.environ["HERMES_DATA_DIR"]) / "foundation-render"
render_root.mkdir()
render_calls = []
def fake_render(args, **kwargs):
    render_calls.append(args[0])
    html_text = Path(args[-1][7:]).read_text(encoding="utf-8")
    pdf_path = Path(next(item.split("=", 1)[1] for item in args if item.startswith("--print-to-pdf=")))
    write_test_pdf(pdf_path, 8 if "body{zoom:1.05}" in html_text else 7)
    return subprocess.CompletedProcess(args, 0)
with patch.object(server.subprocess, "run", side_effect=fake_render):
    fitted_pdf = _render_foundation_pdf("## 模块一", ["/fake/chromium"], render_root)
assert _validate_foundation_pdf(fitted_pdf) == 8
assert render_calls == ["/fake/chromium", "/fake/chromium"]
assert 0.45 in _foundation_zoom_candidates(20)
assert 2.25 in _foundation_zoom_candidates(4)

fallback_root = Path(os.environ["HERMES_DATA_DIR"]) / "foundation-fallback"
fallback_root.mkdir()
fallback_calls = []
def fake_fallback(args, **kwargs):
    fallback_calls.append(args[0])
    if args[0] == "/fake/playwright":
        raise subprocess.TimeoutExpired(args, 60)
    pdf_path = Path(next(item.split("=", 1)[1] for item in args if item.startswith("--print-to-pdf=")))
    write_test_pdf(pdf_path, 8)
    return subprocess.CompletedProcess(args, 0)
with patch.object(server.subprocess, "run", side_effect=fake_fallback):
    fallback_pdf = _render_foundation_pdf("## 模块一", ["/fake/playwright", "/fake/chromium"], fallback_root)
assert _validate_foundation_pdf(fallback_pdf) == 8
assert fallback_calls == ["/fake/playwright", "/fake/chromium"]

anonymous = app.test_client()
assert anonymous.get("/healthz").status_code == 200
assert anonymous.get("/").status_code == 401
client = app.test_client()
client.environ_base["HTTP_AUTHORIZATION"] = "Bearer admin-token"
for path in ("/", "/classic", "/skills", "/analytics", "/images", "/videos",
             "/video-factory", "/pipeline", "/agnes-lab", "/team-workbench"):
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)

created_response = client.post(
    "/api/conversations", json={"title": "CLI 客户诊断"},
    headers={"X-Request-ID": "hermes_runtime_1234"},
)
assert created_response.headers["X-Request-ID"] == "hermes_runtime_1234"
created = created_response.get_json()
cid = created["id"]
audit_rows = [json.loads(line) for line in (Path(os.environ["HERMES_DATA_DIR"]) / "audit" / "security.jsonl").read_text().splitlines()]
created_audit = [row for row in audit_rows if row.get("request_id") == "hermes_runtime_1234"][-1]
assert created_audit["username"] == "admin"
assert created_audit["status"] == 200
assert created_audit["duration_ms"] >= 0
created_response.close()
assert security._active.get("admin", 0) == 0, security._active
owned = client.get(f"/api/conversations/{cid}").get_json()
assert owned["id"] == cid and owned["owner_account_id"] == "acct_a" and owned["title"] == "CLI 客户诊断"
assert client.post("/api/conversations", json={"unknown": True}).status_code == 400
assert client.get(f"/api/conversations/{cid}/reports").get_json() == {}
assert client.get(f"/api/conversations/{cid}/deliverables").get_json() == {}
server.current_account_id = lambda: "acct_b"
assert client.get(f"/api/conversations/{cid}").status_code == 404
assert client.get(f"/api/foundation-report/{cid}.pdf").status_code == 404
assert client.post("/api/foundation-report/generate", json={"conversation_id": cid}).status_code == 404
assert client.post("/api/foundation-report/confirm", json={"conversation_id": cid}).status_code == 404
assert client.get("/api/conversations").get_json() == []
server.current_account_id = lambda: "acct_a"
assert client.delete(f"/api/conversations/{cid}").get_json()["ok"] is True

# Production records stay in the existing conversation JSON.  The bridge is
# mocked here: this route layer must preserve its quote token, source version,
# and one idempotency key without recreating Auth/CLI billing behavior.
production_cid = "production001"
production_state = server.initial_coach_state()
server.save_conversation(production_cid, {
    "id": production_cid, "title": "生产项目", "messages": [],
    "coach_state": production_state, "reports": {}, "owner_account_id": "acct_a",
    "deliverables": {"6": {"kind": "content_pack_v1", "categories": [{
        "id": "category_1", "name": "内容种类", "topics": [{
            "id": "topic_1", "title": "精选选题", "status": "ready",
            "versions": [{"version": 1, "content": "这是可制作的完整口播正文。"}],
        }],
    }]}},
})
revision = production_state["revision"]
intent_response = client.post("/api/chat-complete", json={
    "conversation_id": production_cid,
    "message": "用 Grok 把这篇做成视频",
    "content_target": {"category_id": "category_1", "topic_id": "topic_1"},
    "expected_revision": revision,
    "request_id": "prepare-video-from-chat",
})
assert intent_response.status_code == 200, intent_response.get_data(as_text=True)
intent_body = intent_response.get_json()
assert intent_body["actions"][0]["type"] == "prepare_production", intent_body
assert intent_body["actions"][0]["requested_result"] == "video", intent_body
assert intent_body["actions"][0]["preferred_action"] == "video-generate", intent_body
assert not server.load_conversation(production_cid).get("productions"), intent_body
revision = intent_body["state"]["revision"]
prepared = client.post("/api/ip12/productions/prepare", json={
    "conversation_id": production_cid,
    "content_target": {"category_id": "category_1", "topic_id": "topic_1"},
    "expected_revision": revision, "requested_result": "video",
})
assert prepared.status_code == 200, prepared.get_data(as_text=True)
prepared_body = prepared.get_json()
production_id = prepared_body["production_id"]
assert prepared_body["status"] == "blocked_prerequisite"
bridge_calls = []
def fake_production_bridge(account_id, action, input_body, **kwargs):
    bridge_calls.append((account_id, action, input_body, kwargs))
    if kwargs.get("confirm"):
        return {"job_id": "456", "status": "queued"}
    if action == "task":
        return {"job_id": "456", "status": "done", "asset_refs": [{"id": "asset-1", "kind": "video"}]}
    return {"quote_token": "private-quote-token", "cost": 12, "points": 12, "expires_in": 300}
with patch.object(server, "_bridge_action", side_effect=fake_production_bridge):
    quoted = client.post("/api/ip12/productions/quote", json={
        "conversation_id": production_cid, "production_id": production_id,
        "expected_revision": revision, "options": {"avatar_id": 1, "voice": "voice-demo"},
    })
    assert quoted.status_code == 200, quoted.get_data(as_text=True)
    assert quoted.get_json()["cost"] == 12
    confirmed = client.post("/api/ip12/productions/confirm", json={
        "conversation_id": production_cid, "production_id": production_id,
        "expected_revision": revision, "confirmation_id": "confirm-production-001",
    })
    assert confirmed.status_code == 200, confirmed.get_data(as_text=True)
    assert confirmed.get_json()["production"]["job_id"] == "456"
    restored = client.get(f"/api/ip12/productions/{production_id}?conversation_id={production_cid}")
    assert restored.status_code == 200, restored.get_data(as_text=True)
    assert restored.get_json()["production"]["status"] == "done"
    assert restored.get_json()["production"]["asset_refs"][0]["id"] == "asset-1"
assert len([call for call in bridge_calls if call[3].get("confirm")]) == 1
stored_production = server.load_conversation(production_cid)["productions"][production_id]
assert stored_production["quote"]["token"] == "private-quote-token"
public_project = client.get(f"/api/conversations/{production_cid}").get_json()
assert "private-quote-token" not in json.dumps(public_project, ensure_ascii=False)
assert public_project["productions"][0]["job_id"] == "456"

# A lost confirm response leaves the project in submitting.  Status recovery
# must reuse the same quote and idempotency key, not open another production.
recovery_prepare = client.post("/api/ip12/productions/prepare", json={
    "conversation_id": production_cid,
    "content_target": {"category_id": "category_1", "topic_id": "topic_1"},
    "expected_revision": revision, "requested_result": "video",
}).get_json()
recovery_id = recovery_prepare["production_id"]
recovery_confirm_calls = []
def recovery_bridge(account_id, action, input_body, **kwargs):
    if not kwargs.get("confirm"):
        return {"quote_token": "recovery-quote", "cost": 12, "points": 12, "expires_in": 300}
    recovery_confirm_calls.append(kwargs["idempotency_key"])
    if len(recovery_confirm_calls) == 1:
        raise RuntimeError("response lost")
    return {"job_id": "789", "status": "queued"}
with patch.object(server, "_bridge_action", side_effect=recovery_bridge):
    assert client.post("/api/ip12/productions/quote", json={
        "conversation_id": production_cid, "production_id": recovery_id,
        "expected_revision": revision, "options": {"avatar_id": 2, "voice": "voice-demo"},
    }).status_code == 200
    pending = client.post("/api/ip12/productions/confirm", json={
        "conversation_id": production_cid, "production_id": recovery_id,
        "expected_revision": revision, "confirmation_id": "confirm-production-002",
    })
    assert pending.status_code == 202
    recovered = client.get(f"/api/ip12/productions/{recovery_id}?conversation_id={production_cid}")
    assert recovered.status_code == 200
    assert recovered.get_json()["production"]["job_id"] == "789"
assert len(recovery_confirm_calls) == 2 and len(set(recovery_confirm_calls)) == 1
server.current_account_id = lambda: "acct_b"
assert client.get(f"/api/ip12/productions/{production_id}?conversation_id={production_cid}").status_code == 404
server.current_account_id = lambda: "acct_a"
stale_project = server.load_conversation(production_cid)
stale_project["deliverables"]["6"]["categories"][0]["topics"][0]["versions"].append(
    {"version": 2, "content": "这是一版新的完整口播正文。"}
)
server.save_conversation(production_cid, stale_project)
stale = client.get(f"/api/ip12/productions/{production_id}?conversation_id={production_cid}")
assert stale.status_code == 200
# A completed result remains attached to the version it actually produced;
# only an unconsumed quote is invalidated by a later script version.
assert stale.get_json()["production"]["status"] == "done"

older_cid = "ffffffffffff"
newer_cid = "000000000000"
for ordered_cid, title in ((older_cid, "较早诊断"), (newer_cid, "最近诊断")):
    server.save_conversation(ordered_cid, {
        "id": ordered_cid,
        "title": title,
        "messages": [],
        "coach_state": server.initial_coach_state(),
        "reports": {},
        "deliverables": {},
        "owner_account_id": "acct_a",
    })
os.utime(server.conversation_path(older_cid), (100, 100))
os.utime(server.conversation_path(newer_cid), (200, 200))
ordered_convos = client.get("/api/conversations").get_json()
assert [item["id"] for item in ordered_convos[:2]] == [newer_cid, older_cid], ordered_convos
assert client.delete(f"/api/conversations/{older_cid}").status_code == 200
assert client.delete(f"/api/conversations/{newer_cid}").status_code == 200

raw_pack = {"categories": [{
    "name": f"种类{category}",
    "description": f"这是种类{category}中最值得优先发布的选题。",
    "topics": [{
        "title": f"种类{category}选题01",
        "hook": "开头钩子",
        "objective": "建立信任",
        "script": (f"这是种类{category}精选选题的完整口播文案，包含自然开头、清晰观点、具体解释和克制的行动引导。") * 6,
    }],
} for category in range(1, 4)]}

advance_source = "\n\n".join(
    "### %s\n%s" % (
        name,
        "\n".join("%d. %s选题%02d" % (index, name, index) for index in range(1, 11)),
    )
    for name in ("转行经验分享", "智能体应用实践", "垂直行业真实验证")
)

advance_action_cid = client.post("/api/conversations").get_json()["id"]
advance_action_convo = server.load_conversation(advance_action_cid)
advance_action_state = server.coach_harness.initial_state()
advance_action_state.update(
    current_module=5,
    module_step=1,
    completed_modules=[1, 2, 3, 4],
    foundation_report={"status": "confirmed"},
    pending={
        "id": "topics-ready",
        "kind": "checkpoint",
        "status": "awaiting_confirmation",
        "module": 5,
        "step": 2,
        "draft": advance_source,
        "self_review": "完整 3×10。",
        "profile_updates": [],
        "confidence": 1,
    },
)
advance_action_state["intake"]["status"] = "complete"
advance_action_convo["coach_state"] = advance_action_state
server.save_conversation(advance_action_cid, advance_action_convo)
with patch.object(server, "call_ai") as advance_model:
    advance_action = client.post("/api/chat-complete", json={
        "conversation_id": advance_action_cid,
        "action": {"type": "confirm_checkpoint", "target_id": "topics-ready"},
        "expected_revision": advance_action_state["revision"],
    })
    assert advance_action.status_code == 200, advance_action.get_data(as_text=True)
    advance_model.assert_not_called()
advance_action_json = advance_action.get_json()
assert advance_action_json["state"]["module_step"] == 2
assert advance_action_json["state"]["pending"]["step"] == 3
assert "首批 6 条发布顺序" in advance_action_json["assistant"]

advance_retry_cid = client.post("/api/conversations").get_json()["id"]
advance_retry_convo = server.load_conversation(advance_retry_cid)
advance_retry_state = server.coach_harness.initial_state()
advance_retry_state.update(
    current_module=5,
    module_step=2,
    completed_modules=[1, 2, 3, 4],
    foundation_report={"status": "confirmed"},
)
advance_retry_state["intake"]["status"] = "complete"
advance_retry_state["ip_profile"]["confirmed_outputs"]["5-2"] = {"content": advance_source}
advance_retry_convo["coach_state"] = advance_retry_state
server.save_conversation(advance_retry_cid, advance_retry_convo)
with patch.object(server, "call_ai") as retry_model:
    advance_retry = client.post("/api/chat-complete", json={
        "conversation_id": advance_retry_cid,
        "message": "下一步",
        "expected_revision": advance_retry_state["revision"],
    })
    assert advance_retry.status_code == 200, advance_retry.get_data(as_text=True)
    retry_model.assert_not_called()
assert advance_retry.get_json()["state"]["pending"]["step"] == 3

module_six_convo = server.load_conversation(advance_retry_cid)
module_six_state = module_six_convo["coach_state"]
module_six_state.update(current_module=6, module_step=0, completed_modules=[1, 2, 3, 4, 5])
module_six_convo["coach_state"] = module_six_state
server.save_conversation(advance_retry_cid, module_six_convo)
with patch.object(server, "call_ai") as module_six_entry_model:
    module_six_entry_response = client.post("/api/chat-complete", json={
        "conversation_id": advance_retry_cid,
        "message": "下一步",
        "expected_revision": module_six_state["revision"],
    })
    module_six_entry_model.assert_not_called()
assert module_six_entry_response.status_code == 200, module_six_entry_response.get_data(as_text=True)
module_six_entry = module_six_entry_response.get_json()
assert module_six_entry["state"]["current_module"] == 6
assert module_six_entry["state"]["module_step"] == 0
assert "表达风格" in module_six_entry["assistant"]
assert "每篇大约多长" in module_six_entry["assistant"]
module_six_exact_convo = server.load_conversation(advance_retry_cid)
module_six_exact_state = module_six_exact_convo["coach_state"]
module_six_exact_convo.setdefault("messages", []).extend([
    {"role": "user", "content": "1min"},
    {"role": "assistant", "content": "你希望口播偏口语化还是正式？最后希望观众关注、评论、私信，还是去试 AI 工具？"},
])
server.save_conversation(advance_retry_cid, module_six_exact_convo)
exact_preference = "我希望口播是偏口语化的，就像和好友在聊天，我希望观众可以点赞、评论我"
with patch.object(server, "call_ai") as exact_preference_model:
    exact_preference_response = client.post("/api/chat-complete", json={
        "conversation_id": advance_retry_cid,
        "message": exact_preference,
        "expected_revision": module_six_exact_state["revision"],
    })
    exact_preference_model.assert_not_called()
assert exact_preference_response.status_code == 200, exact_preference_response.get_data(as_text=True)
exact_preference_json = exact_preference_response.get_json()
assert exact_preference_json["state"]["pending"]["step"] == 1
assert "点赞、评论" in exact_preference_json["state"]["pending"]["draft"]

fallback_cid = client.post("/api/conversations").get_json()["id"]
fallback_convo = server.load_conversation(fallback_cid)
fallback_state = server.coach_harness.initial_state()
fallback_state["intake"]["status"] = "complete"
fallback_convo["coach_state"] = fallback_state
server.save_conversation(fallback_cid, fallback_convo)
fallback_words = "这是模型暂时无法整理、但必须先保存的用户原话"
seen_before_model = []
def fail_before_model(snapshot, user_message, repair_error=""):
    saved = server.load_conversation(fallback_cid).get("messages", [])
    seen_before_model.append(any(
        item.get("role") == "user" and item.get("content") == fallback_words
        for item in saved
    ))
    raise RuntimeError("temporary model failure")

with patch.object(server, "_coach_model_decision", side_effect=fail_before_model):
    fallback_response = client.post("/api/chat-complete", json={
        "conversation_id": fallback_cid,
        "message": fallback_words,
        "expected_revision": fallback_state["revision"],
        "request_id": "fallback-persists-once",
    })
assert fallback_response.status_code == 200, fallback_response.get_data(as_text=True)
assert seen_before_model == [True], seen_before_model
fallback_json = fallback_response.get_json()
assert fallback_json["ok"] is True
assert "已经记下你刚才的原话" in fallback_json["assistant"]
saved_fallback = server.load_conversation(fallback_cid)
assert sum(
    item.get("role") == "user" and item.get("content") == fallback_words
    for item in saved_fallback["messages"]
) == 1
assert sum(
    item.get("role") == "user" and item.get("message_id")
    for item in saved_fallback["messages"]
) == 1
assert saved_fallback["messages"][-2]["content"] == fallback_words
assert saved_fallback["messages"][-1]["content"] == fallback_json["assistant"]
assert saved_fallback["coach_state"]["revision"] == fallback_state["revision"] + 1
replayed_fallback = client.post("/api/chat-complete", json={
    "conversation_id": fallback_cid,
    "message": fallback_words,
    "expected_revision": fallback_state["revision"],
    "request_id": "fallback-persists-once",
})
assert replayed_fallback.status_code == 200
assert len(server.load_conversation(fallback_cid)["messages"]) == len(saved_fallback["messages"])
captured_module_six = {}
def capture_module_six(messages, **kwargs):
    captured_module_six["messages"] = messages
    response = Mock()
    response.json.return_value = {"choices": [{"message": {"content": json.dumps({
        "decision": "ask_follow_up", "checkpoint": 0, "reply": "请补充风格。",
        "draft": "", "self_review": "", "profile_updates": [], "confidence": 0.8,
    }, ensure_ascii=False)}}]}
    return response
with patch.object(server, "call_ai", side_effect=capture_module_six):
    server._coach_model_decision(module_six_convo, "模块 6 会怎么使用模块 5 的选题？")
module_six_context = "\n".join(item["content"] for item in captured_module_six["messages"])
assert "confirmed_module_five_plan" in module_six_context
assert "转行经验分享选题01" in module_six_context

content_pack = server._normalize_content_pack(raw_pack)
assert content_pack["kind"] == "content_pack_v1"
assert content_pack["format"] == "featured_3_v1"
assert len(content_pack["categories"]) == 3
assert all(len(category["topics"]) == 1 for category in content_pack["categories"])
assert sum(len(category["topics"]) for category in content_pack["categories"]) == 3
try:
    server._normalize_content_pack({"categories": raw_pack["categories"][:2]})
    raise AssertionError("two-category pack was accepted")
except ValueError:
    pass
short_pack = json.loads(json.dumps(raw_pack, ensure_ascii=False))
short_pack["categories"][0]["topics"][0]["script"] = "只有标题，没有完整正文。"
try:
    server._normalize_content_pack(short_pack)
    raise AssertionError("title-only content was accepted as a complete script")
except ValueError:
    pass

module_six_convo["deliverables"] = {"6": content_pack}
module_six_state["module_step"] = 1
module_six_convo["coach_state"] = module_six_state
with patch.object(server, "call_ai") as module_six_model:
    module_six_review, _ = server._coach_model_decision(module_six_convo, "下一步")
    module_six_model.assert_not_called()
assert module_six_review["checkpoint"] == 2
module_six_state["module_step"] = 2
with patch.object(server, "call_ai") as module_six_model:
    module_six_confirm, _ = server._coach_model_decision(module_six_convo, "下一步")
    module_six_model.assert_not_called()
assert module_six_confirm["checkpoint"] == 3

generated_cid = client.post("/api/conversations").get_json()["id"]
generated_convo = server.load_conversation(generated_cid)
generated_state = server.coach_harness.initial_state()
generated_state.update(
    current_module=6,
    module_step=0,
    completed_modules=[1, 2, 3, 4, 5],
    foundation_report={"status": "confirmed"},
)
generated_state["intake"]["status"] = "complete"
generated_state["ip_profile"]["confirmed_outputs"]["5-2"] = {
    "content": "\n\n".join(
        "### %s\n%s" % (
            category["name"],
            "\n".join(
                "%d. %s选题%02d" % (index, category["name"], index)
                for index in range(1, 11)
            ),
        )
        for category in raw_pack["categories"]
    )
}
generated_convo["coach_state"] = generated_state
server.save_conversation(generated_cid, generated_convo)
pack_response = Mock()
pack_response.json.return_value = {"choices": [{"message": {"content": json.dumps(raw_pack, ensure_ascii=False)}}]}
with patch.object(server, "call_ai", return_value=pack_response) as pack_model:
    generated_pack = server.generate_deliverable(generated_cid, 6)
assert len(generated_pack["categories"]) == 3
assert sum(len(category["topics"]) for category in generated_pack["categories"]) == 3
assert server.load_conversation(generated_cid)["deliverables"]["6"]["kind"] == "content_pack_v1"
assert pack_model.call_args.kwargs["response_format"]["type"] == "json_schema"

drifted_raw_pack = json.loads(json.dumps(raw_pack, ensure_ascii=False))
for category in drifted_raw_pack["categories"]:
    category["name"] += "方向"
    category["topics"][0]["title"] += "完整版"
drifted_response = Mock()
drifted_response.json.return_value = {
    "choices": [{"message": {"content": json.dumps(drifted_raw_pack, ensure_ascii=False)}}]
}
with patch.object(server, "call_ai", return_value=drifted_response):
    repaired_pack = server._generate_content_pack(generated_convo)
assert [item["name"] for item in repaired_pack["categories"]] == [
    item["name"] for item in raw_pack["categories"]
]
assert [item["topics"][0]["title"] for item in repaired_pack["categories"]] == [
    item["topics"][0]["title"] for item in raw_pack["categories"]
]

legacy_cid = client.post("/api/conversations").get_json()["id"]
legacy_convo = server.load_conversation(legacy_cid)
legacy_state = json.loads(json.dumps(generated_state, ensure_ascii=False))
legacy_state["module_step"] = 1
legacy_state["pending"] = None
legacy_convo["coach_state"] = legacy_state
legacy_convo["deliverables"] = {"6": {
    "kind": "content_pack_v1",
    "title": "📝 3×10 口播内容库",
    "categories": [{
        "id": f"category-{category}",
        "name": f"种类{category}",
        "topics": [{
            "id": f"topic-{category}-{topic:02d}",
            "title": f"种类{category}选题{topic:02d}",
            "versions": [{"version": 1, "content": "旧版正文"}],
        } for topic in range(1, 11)],
    } for category in range(1, 4)],
}}
server.save_conversation(legacy_cid, legacy_convo)
with patch.object(server, "call_ai", return_value=pack_response) as legacy_pack_model:
    legacy_review = client.post("/api/chat-complete", json={
        "conversation_id": legacy_cid,
        "message": "口播文案我先看看",
        "content_target": {"category_id": "category-1", "topic_id": "topic-1-01"},
        "expected_revision": legacy_state["revision"],
    })
assert legacy_review.status_code == 200, legacy_review.get_data(as_text=True)
legacy_review_json = legacy_review.get_json()
assert "3 篇完整口播文案" in legacy_review_json["assistant"]
assert "这是种类1精选选题的完整口播文案" in legacy_review_json["assistant"]
assert legacy_pack_model.call_count == 1
assert server.load_conversation(legacy_cid)["deliverables"]["6"]["format"] == "featured_3_v1"

content_cid = client.post("/api/conversations").get_json()["id"]
content_convo = server.load_conversation(content_cid)
content_convo["deliverables"] = {"6": content_pack}
server.save_conversation(content_cid, content_convo)
content_state = client.get(f"/api/conversations/{content_cid}").get_json()["coach_state"]
revision_response = Mock()
revision_response.json.return_value = {"choices": [{"message": {"content": json.dumps({
    "decision": "apply_revision",
    "reply": "刚才开头太绕，我已经改成直接说结论。",
    "change_summary": "缩短开头",
    "revised_script": "先说结论：这是修改后的口播文案。",
}, ensure_ascii=False)}}]}
target = {"category_id": "category-1", "topic_id": "topic-1-01"}
with patch.object(server, "call_ai", return_value=revision_response):
    content_revision = client.post("/api/chat-complete", json={
        "conversation_id": content_cid,
        "message": "开头太绕了，直接一点。",
        "content_target": target,
        "expected_revision": content_state["revision"],
    })
assert content_revision.status_code == 200, content_revision.get_data(as_text=True)
updated_pack = server.load_conversation(content_cid)["deliverables"]["6"]
updated_topic = updated_pack["categories"][0]["topics"][0]
assert [item["version"] for item in updated_topic["versions"]] == [1, 2]
assert updated_topic["versions"][-1]["content"].startswith("先说结论")
assert len(updated_pack["categories"][1]["topics"][0]["versions"]) == 1
assert content_revision.get_json()["auto_deliverables"]["6"]["categories"][0]["topics"][0]["status"] == "revised"
assert client.post("/api/chat-complete", json={
    "conversation_id": content_cid,
    "message": "修改",
    "content_target": {"category_id": "category-9", "topic_id": "topic-9-99"},
    "expected_revision": content_revision.get_json()["state"]["revision"],
}).status_code == 400

foundation_cid = client.post("/api/conversations").get_json()["id"]
assert client.post("/api/foundation-report/confirm", json={"conversation_id": foundation_cid}).status_code == 409
assert client.post("/api/jump-module", json={"conversation_id": foundation_cid, "module": 5}).status_code == 409
assert client.post("/api/foundation-report/generate", json={"conversation_id": foundation_cid}).status_code == 409

gated = server.load_conversation(foundation_cid)
gated["coach_state"] = {"current_module": 4, "completed_modules": [1, 2, 3, 4],
                         "module_step": 0, "foundation_report": {"status": "generating"}}
server.save_conversation(foundation_cid, gated)
with patch.object(server, "call_ai") as gated_model:
    gated_reply = client.post("/api/chat-complete", json={"conversation_id": foundation_cid, "message": "继续"})
    assert gated_reply.status_code == 200, gated_reply.get_data(as_text=True)
    assert "已经保存" in gated_reply.get_json()["assistant"]
    gated_model.assert_not_called()
gated = server.load_conversation(foundation_cid)
assert sum(item["role"] == "user" and item["content"] == "继续" for item in gated["messages"]) == 1
assert client.post("/api/generate-report", json={"conversation_id": foundation_cid, "module": 5}).status_code == 409
assert client.post("/api/generate-deliverable", json={"conversation_id": foundation_cid, "module": 5}).status_code == 409
assert client.post("/api/jump-module", json={"conversation_id": foundation_cid, "module": 7}).status_code == 409
assert client.post("/api/generate-report", json={"conversation_id": foundation_cid, "module": 7}).status_code == 409
assert client.post("/api/generate-deliverable", json={"conversation_id": foundation_cid, "module": 7}).status_code == 409
for coming_soon_path in ("/api/module7-images", "/api/module8-video", "/api/m9-funnel", "/api/m11-sales", "/api/m12-calendar"):
    assert client.post(coming_soon_path, json={}).status_code == 409, coming_soon_path

gated = server.load_conversation(foundation_cid)
gated["coach_state"] = {"current_module": 8, "completed_modules": list(range(1, 8)),
                         "module_step": 3, "foundation_report": {"status": "awaiting_confirmation"}}
server.save_conversation(foundation_cid, gated)
legacy_detail = client.get(f"/api/conversations/{foundation_cid}").get_json()["coach_state"]
assert legacy_detail["current_module"] == 6, legacy_detail
assert legacy_detail["completed_modules"] == [1, 2, 3, 4, 5, 6], legacy_detail
assert legacy_detail["module_step"] == 0, legacy_detail
server.FOUNDATION_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
foundation_pdf = server.FOUNDATION_REPORTS_DIR / f"{foundation_cid}.pdf"
foundation_pdf.unlink(missing_ok=True)
assert client.get(f"/api/foundation-report/{foundation_cid}.pdf").status_code == 404
gated = server.load_conversation(foundation_cid)
assert gated["coach_state"]["foundation_report"]["status"] == "failed"
gated["coach_state"]["foundation_report"] = {"status": "awaiting_confirmation"}
server.save_conversation(foundation_cid, gated)
foundation_pdf.write_bytes(invalid_pdf.read_bytes())
assert client.get(f"/api/foundation-report/{foundation_cid}.pdf").status_code == 409
gated = server.load_conversation(foundation_cid)
assert gated["coach_state"]["foundation_report"]["status"] == "failed"
gated["coach_state"]["foundation_report"] = {"status": "awaiting_confirmation"}
server.save_conversation(foundation_cid, gated)
foundation_pdf.write_bytes(invalid_pdf.read_bytes())
assert client.post("/api/foundation-report/confirm", json={"conversation_id": foundation_cid}).status_code == 409
gated = server.load_conversation(foundation_cid)
assert gated["coach_state"]["foundation_report"]["status"] == "failed"
gated["coach_state"]["foundation_report"] = {"status": "awaiting_confirmation"}
server.save_conversation(foundation_cid, gated)
foundation_pdf.write_bytes(valid_pdf.read_bytes())
with patch.object(server, "call_ai") as report_model:
    duplicate = client.post("/api/foundation-report/generate", json={"conversation_id": foundation_cid})
    assert duplicate.status_code == 409, duplicate.get_data(as_text=True)
    report_model.assert_not_called()
download = client.get(f"/api/foundation-report/{foundation_cid}.pdf")
assert download.status_code == 200
assert download.headers["Content-Disposition"].startswith("attachment;")
assert download.headers["Cache-Control"] == "private, no-store"
preview = client.get(f"/api/foundation-report/{foundation_cid}.pdf?preview=1")
assert preview.status_code == 200
assert preview.headers["Content-Disposition"].startswith("inline;")
assert preview.headers["Cache-Control"] == "private, no-store"
confirmed = client.post("/api/foundation-report/confirm", json={"conversation_id": foundation_cid})
assert confirmed.status_code == 200, confirmed.get_data(as_text=True)
assert confirmed.get_json()["state"]["current_module"] == 6
assert confirmed.get_json()["state"]["module_step"] == 0

normal_confirm_cid = client.post("/api/conversations").get_json()["id"]
normal_confirm = server.load_conversation(normal_confirm_cid)
normal_confirm["coach_state"] = {"current_module": 4, "completed_modules": [1, 2, 3, 4],
                                   "module_step": 4, "foundation_report": {"status": "awaiting_confirmation"}}
server.save_conversation(normal_confirm_cid, normal_confirm)
(server.FOUNDATION_REPORTS_DIR / f"{normal_confirm_cid}.pdf").write_bytes(valid_pdf.read_bytes())
confirmed = client.post("/api/foundation-report/confirm", json={"conversation_id": normal_confirm_cid})
assert confirmed.get_json()["state"]["current_module"] == 5
assert confirmed.get_json()["state"]["module_step"] == 0

review_cid = client.post("/api/conversations").get_json()["id"]
review_convo = server.load_conversation(review_cid)
review_convo["coach_state"] = {
    "current_module": 4,
    "completed_modules": [1, 2, 3, 4],
    "module_step": 5,
    "foundation_report": {
        "status": "awaiting_confirmation",
        "report_id": "report-old",
        "content": "## 模块四｜故事资产挖掘\n候选故事线｜待本人补充",
        "review_status": "clean",
        "review_notes": [],
    },
}
server.save_conversation(review_cid, review_convo)
(server.FOUNDATION_REPORTS_DIR / f"{review_cid}.pdf").write_bytes(valid_pdf.read_bytes())
review_state = client.get(f"/api/conversations/{review_cid}").get_json()["coach_state"]
qa_decision = {
    "decision": "answer_only", "checkpoint": 0,
    "reply": "这里标记待本人补充，是因为原对话没有足够的真实故事事实。",
    "draft": "", "self_review": "", "profile_updates": [], "confidence": 0.9,
}
qa_response = Mock()
qa_response.json.return_value = {"choices": [{"message": {"content": json.dumps(qa_decision, ensure_ascii=False)}}]}
with patch.object(server, "call_ai", return_value=qa_response) as qa_model:
    qa_reply = client.post("/api/chat-complete", json={
        "conversation_id": review_cid,
        "message": "为什么这里写待本人补充？",
        "expected_revision": review_state["revision"],
        "request_id": "foundation-review-question",
    })
assert qa_reply.status_code == 200, qa_reply.get_data(as_text=True)
assert qa_reply.get_json()["assistant"].startswith("这里标记待本人补充")
assert qa_reply.get_json()["state"]["current_module"] == 4
model_messages = qa_model.call_args.args[0]
assert any("候选故事线｜待本人补充" in item.get("content", "") for item in model_messages)

revision_state = qa_reply.get_json()["state"]
revision_decision = Mock()
revision_decision.json.return_value = {"choices": [{"message": {"content": json.dumps({
    "decision": "apply_revision",
    "reply": "已理解：把真实转折补入故事资产。请重新生成 PDF 后查看新版。",
    "revision_note": "在故事资产中补充：第一次创业失败后重新开始。",
}, ensure_ascii=False)}}]}
with patch.object(server, "call_ai", return_value=revision_decision) as revision_model:
    revised = client.post("/api/chat-complete", json={
        "conversation_id": review_cid,
        "message": "我的真实转折是第一次创业失败后重新开始。",
        "foundation_review": "revision",
        "expected_revision": revision_state["revision"],
        "request_id": "foundation-review-revision",
    })
assert revised.status_code == 200, revised.get_data(as_text=True)
revision_model.assert_called_once()
dirty_state = revised.get_json()["state"]
assert dirty_state["foundation_report"]["review_status"] == "dirty"
assert dirty_state["foundation_report"]["review_notes"][-1]["content"] == "在故事资产中补充：第一次创业失败后重新开始。"

question_cid = client.post("/api/conversations").get_json()["id"]
question_convo = server.load_conversation(question_cid)
question_convo["coach_state"] = review_convo["coach_state"]
server.save_conversation(question_cid, question_convo)
question_state = client.get(f"/api/conversations/{question_cid}").get_json()["coach_state"]
question_decision = Mock()
question_decision.json.return_value = {"choices": [{"message": {"content": json.dumps({
    "decision": "answer_only",
    "reply": "这是因为原对话里没有足够的真实故事事实；如果你不需要这一项，可以要求删除。",
    "revision_note": "",
}, ensure_ascii=False)}}]}
with patch.object(server, "call_ai", return_value=question_decision):
    questioned = client.post("/api/chat-complete", json={
        "conversation_id": question_cid,
        "message": "为什么后面总有待本人补充？",
        "foundation_review": "revision",
        "expected_revision": question_state["revision"],
    })
assert questioned.status_code == 200, questioned.get_data(as_text=True)
questioned_state = questioned.get_json()["state"]
assert questioned_state["foundation_report"]["review_status"] == "clean"
assert questioned_state["foundation_report"]["review_notes"] == []

vague_decision = Mock()
vague_decision.json.return_value = {"choices": [{"message": {"content": json.dumps({
    "decision": "ask_follow_up",
    "reply": "具体是哪一段不对？请说出要删除、补充或改成的内容。",
    "revision_note": "",
}, ensure_ascii=False)}}]}
with patch.object(server, "call_ai", return_value=vague_decision):
    vague = client.post("/api/chat-complete", json={
        "conversation_id": question_cid,
        "message": "这里不对。",
        "foundation_review": "revision",
        "expected_revision": questioned_state["revision"],
    })
assert vague.status_code == 200, vague.get_data(as_text=True)
assert vague.get_json()["state"]["foundation_report"]["review_status"] == "clean"

invalid_decision = Mock()
invalid_decision.json.return_value = {"choices": [{"message": {"content": "{}"}}]}
with patch.object(server, "call_ai", return_value=invalid_decision):
    invalid_review = client.post("/api/chat-complete", json={
        "conversation_id": question_cid,
        "message": "请判断这一段。",
        "foundation_review": "revision",
        "expected_revision": vague.get_json()["state"]["revision"],
    })
assert invalid_review.status_code == 502
assert server.load_conversation(question_cid)["coach_state"]["foundation_report"]["review_status"] == "clean"

empty_note_decision = Mock()
empty_note_decision.json.return_value = {"choices": [{"message": {"content": json.dumps({
    "decision": "apply_revision",
    "reply": "我会修改这一段。",
    "revision_note": "",
}, ensure_ascii=False)}}]}
with patch.object(server, "call_ai", return_value=empty_note_decision):
    empty_note_review = client.post("/api/chat-complete", json={
        "conversation_id": question_cid,
        "message": "请删除这个待填写项。",
        "foundation_review": "revision",
        "expected_revision": vague.get_json()["state"]["revision"],
    })
assert empty_note_review.status_code == 502
assert server.load_conversation(question_cid)["coach_state"]["foundation_report"]["review_status"] == "clean"

conflict_state = vague.get_json()["state"]
def bump_review_revision(*_args, **_kwargs):
    changed = server.load_conversation(question_cid)
    changed["coach_state"]["revision"] += 1
    server.save_conversation(question_cid, changed)
    return question_decision

with patch.object(server, "call_ai", side_effect=bump_review_revision):
    conflict_review = client.post("/api/chat-complete", json={
        "conversation_id": question_cid,
        "message": "为什么这里待填写？",
        "foundation_review": "revision",
        "expected_revision": conflict_state["revision"],
    })
assert conflict_review.status_code == 409
assert "另一端更新" in conflict_review.get_json()["error"]

deleted_cid = client.post("/api/conversations").get_json()["id"]
deleted_convo = server.load_conversation(deleted_cid)
deleted_convo["coach_state"] = review_convo["coach_state"]
server.save_conversation(deleted_cid, deleted_convo)
deleted_state = client.get(f"/api/conversations/{deleted_cid}").get_json()["coach_state"]
def delete_during_review(*_args, **_kwargs):
    server.conversation_path(deleted_cid).unlink()
    return question_decision

with patch.object(server, "call_ai", side_effect=delete_during_review):
    deleted_review = client.post("/api/chat-complete", json={
        "conversation_id": deleted_cid,
        "message": "为什么这里待填写？",
        "foundation_review": "revision",
        "expected_revision": deleted_state["revision"],
    })
assert deleted_review.status_code == 404
blocked_confirm = client.post("/api/foundation-report/confirm", json={
    "conversation_id": review_cid,
    "expected_revision": dirty_state["revision"],
    "report_id": "report-old",
})
assert blocked_confirm.status_code == 409
assert "重新生成" in blocked_confirm.get_json()["error"]

report_response = Mock()
report_response.json.return_value = {"choices": [{"message": {"content": "## 模块一｜定位诊断\n新版报告"}}]}
with patch.object(server, "call_ai", return_value=report_response) as regenerate_model, \
        patch.object(server, "_render_foundation_pdf", return_value=valid_pdf):
    regenerated = client.post("/api/foundation-report/generate", json={"conversation_id": review_cid})
assert regenerated.status_code == 200, regenerated.get_data(as_text=True)
new_state = regenerated.get_json()["state"]
assert new_state["foundation_report"]["report_id"] != "report-old"
assert new_state["foundation_report"]["review_status"] == "clean"
assert new_state["foundation_report"]["review_notes"] == []
report_messages = regenerate_model.call_args.args[0]
assert regenerate_model.call_args.kwargs["max_tokens"] == 16000
assert any("第一次创业失败后重新开始" in item.get("content", "") for item in report_messages)
assert "不创建‘待补充’故事凑数" in report_messages[0]["content"]
assert "不强制凑数量" in report_messages[0]["content"]

empty_report_cid = client.post("/api/conversations").get_json()["id"]
empty_report = server.load_conversation(empty_report_cid)
empty_report["coach_state"] = {"current_module": 4, "completed_modules": [1, 2, 3, 4],
                               "module_step": 4, "foundation_report": {"status": "failed"}}
server.save_conversation(empty_report_cid, empty_report)
empty_response = Mock()
empty_response.json.return_value = {"choices": [{"message": {"content": ""}}]}
with patch.object(server, "call_ai", return_value=empty_response), \
        patch.object(server, "_render_foundation_pdf") as empty_renderer:
    empty_generation = client.post("/api/foundation-report/generate", json={"conversation_id": empty_report_cid})
assert empty_generation.status_code == 502
empty_renderer.assert_not_called()
assert server.load_conversation(empty_report_cid)["coach_state"]["foundation_report"]["error"] == "AI report is empty"

stale_confirm = client.post("/api/foundation-report/confirm", json={
    "conversation_id": review_cid,
    "expected_revision": new_state["revision"],
    "report_id": "report-old",
})
assert stale_confirm.status_code == 409
latest_report_id = new_state["foundation_report"]["report_id"]
latest_confirm = client.post("/api/foundation-report/confirm", json={
    "conversation_id": review_cid,
    "expected_revision": new_state["revision"],
    "report_id": latest_report_id,
})
assert latest_confirm.status_code == 200, latest_confirm.get_data(as_text=True)
assert latest_confirm.get_json()["state"]["current_module"] == 5

owned_video = artifact_store.video_path("admin", "0123456789.mp4")
owned_video.parent.mkdir(parents=True, exist_ok=True)
owned_video.write_bytes(b"video")
assert client.get(
    "/api/video-file/0123456789.mp4",
    headers={"Authorization": "Bearer admin-token"},
).status_code == 200
assert client.get(
    "/api/video-file/0123456789.mp4",
    headers={"Authorization": "Bearer member-a-token"},
).status_code == 404
assert client.get(
    "/api/video-file/../../0123456789.mp4",
    headers={"Authorization": "Bearer admin-token"},
).status_code == 404

security._rate_hits.clear()
original_rate = security.RATE_REQUESTS
security.RATE_REQUESTS = 1
assert client.post("/api/humanize", json={"text": ""}).status_code == 400
assert client.post("/api/humanize", json={"text": ""}).status_code == 429
security.RATE_REQUESTS = original_rate
security._rate_hits.clear()

security._active["admin"] = security.USER_CONCURRENCY
assert client.post("/api/humanize", json={"text": ""}).status_code == 429
security._active.clear()

original_quota = artifact_store.DATA_QUOTA_BYTES
artifact_store.DATA_QUOTA_BYTES = 1
assert client.post("/api/media/upload", json={"data": "AAAA"}).status_code == 507
artifact_store.DATA_QUOTA_BYTES = original_quota
quota_first = artifact_store.media_path("admin", artifact_store.new_asset_id(), ".bin")
quota_second = artifact_store.media_path("admin", artifact_store.new_asset_id(), ".bin")
artifact_store.DATA_QUOTA_BYTES = artifact_store.directory_size() + 5
artifact_store.atomic_write_bytes(quota_first, b"1234")
try:
    artifact_store.atomic_write_bytes(quota_second, b"5678")
    raise AssertionError("second quota write should fail")
except artifact_store.StorageQuotaExceeded:
    pass
assert quota_first.exists() and not quota_second.exists()
artifact_store.DATA_QUOTA_BYTES = original_quota

# Rollback copies retained in the old flat directories are not counted twice.
canonical_size = artifact_store.directory_size()
legacy_video = Path(os.environ["HERMES_DATA_DIR"]) / "videos" / "legacy.mp4"
legacy_video.parent.mkdir(parents=True, exist_ok=True)
legacy_video.write_bytes(b"legacy" * 10000)
assert artifact_store.directory_size() == canonical_size

# Moving a retained legacy file into canonical storage still requires capacity.
legacy_move = legacy_video.with_name("legacy-move.bin")
legacy_move.write_bytes(b"0123456789")
legacy_destination = artifact_store.media_path(
    "admin", artifact_store.new_asset_id(), ".bin"
)
artifact_store.DATA_QUOTA_BYTES = artifact_store.directory_size() + 5
try:
    artifact_store.finalize_file(legacy_move, legacy_destination)
    raise AssertionError("legacy-to-canonical move should enforce quota")
except artifact_store.StorageQuotaExceeded:
    pass
assert legacy_move.exists() and not legacy_destination.exists()
artifact_store.DATA_QUOTA_BYTES = original_quota

# Cross-filesystem finalize falls back to a target-side atomic copy.
external_root = Path(os.environ["HERMES_DATA_DIR"]).parent
cross_source = external_root / f"hermes-cross-{os.getpid()}.bin"
cross_destination = artifact_store.media_path(
    "admin", artifact_store.new_asset_id(), ".bin"
)
cross_content = b"cross-filesystem-content"
cross_source.write_bytes(cross_content)
real_replace = artifact_store.os.replace
replace_calls = []

def replace_cross_device_once(source, destination):
    replace_calls.append((Path(source), Path(destination)))
    if len(replace_calls) == 1:
        raise OSError(errno.EXDEV, "cross-device link")
    return real_replace(source, destination)

with patch.object(
    artifact_store.os, "replace", side_effect=replace_cross_device_once
):
    artifact_store.finalize_file(cross_source, cross_destination)
assert not cross_source.exists()
assert cross_destination.read_bytes() == cross_content
assert hashlib.sha256(cross_destination.read_bytes()).digest() == hashlib.sha256(
    cross_content
).digest()
assert not list(cross_destination.parent.glob(f".{cross_destination.name}.*.tmp"))

# Copy interruption keeps the source and removes target-side temporary files.
interrupted_source = external_root / f"hermes-interrupted-{os.getpid()}.bin"
interrupted_destination = artifact_store.media_path(
    "admin", artifact_store.new_asset_id(), ".bin"
)
interrupted_source.write_bytes(b"complete-source")

def interrupt_copy(source, destination):
    Path(destination).write_bytes(b"partial")
    raise OSError(errno.EIO, "copy interrupted")

with patch.object(
    artifact_store.os, "replace", side_effect=OSError(errno.EXDEV, "cross-device link")
), patch.object(artifact_store.shutil, "copy2", side_effect=interrupt_copy):
    try:
        artifact_store.finalize_file(interrupted_source, interrupted_destination)
        raise AssertionError("interrupted copy should fail")
    except OSError as exc:
        assert exc.errno == errno.EIO
assert interrupted_source.exists()
assert not interrupted_destination.exists()
assert not list(
    interrupted_destination.parent.glob(f".{interrupted_destination.name}.*.tmp")
)
interrupted_source.unlink()

# Non-cross-device errors fail closed and never enter the copy fallback.
closed_source = external_root / f"hermes-closed-{os.getpid()}.bin"
closed_destination = artifact_store.media_path(
    "admin", artifact_store.new_asset_id(), ".bin"
)
closed_source.write_bytes(b"closed")
with patch.object(
    artifact_store.os, "replace", side_effect=PermissionError(errno.EACCES, "denied")
), patch.object(artifact_store.shutil, "copy2") as forbidden_copy:
    try:
        artifact_store.finalize_file(closed_source, closed_destination)
        raise AssertionError("permission error should fail")
    except PermissionError:
        pass
forbidden_copy.assert_not_called()
assert closed_source.exists() and not closed_destination.exists()
closed_source.unlink()

# Cross-filesystem fallback uses peak, not final-net, quota accounting.
peak_source = external_root / f"hermes-peak-{os.getpid()}.bin"
peak_destination = artifact_store.media_path(
    "admin", artifact_store.new_asset_id(), ".bin"
)
peak_source.write_bytes(b"0123456789")
artifact_store.atomic_write_bytes(peak_destination, b"old-target")
artifact_store.DATA_QUOTA_BYTES = artifact_store.directory_size() + 5
with patch.object(
    artifact_store.os, "replace", side_effect=OSError(errno.EXDEV, "cross-device link")
), patch.object(artifact_store.shutil, "copy2") as quota_copy:
    try:
        artifact_store.finalize_file(peak_source, peak_destination)
        raise AssertionError("cross-device peak quota should fail")
    except artifact_store.StorageQuotaExceeded:
        pass
quota_copy.assert_not_called()
assert peak_source.exists()
assert peak_destination.read_bytes() == b"old-target"
assert not list(peak_destination.parent.glob(f".{peak_destination.name}.*.tmp"))
peak_source.unlink()
artifact_store.DATA_QUOTA_BYTES = original_quota

assert client.post(
    "/api/chat",
    json={"conversation_id": "../../knowledge/visual_formulas", "message": "test"},
).status_code == 400

drift_cid = client.post("/api/conversations").get_json()["id"]
drift_message = "叫我泽龙就好"
with patch.object(server, "_coach_model_decision", return_value=({
    "decision": "ask_follow_up",
    "checkpoint": 1,
    "reply": "好的，泽龙。你现在主要从事什么工作？",
    "draft": "称呼：泽龙",
    "self_review": "仍需本人确认。",
    "profile_updates": [{
        "field": "preferred_name",
        "value": "泽龙",
        "kind": "user_fact",
        "evidence_quote": drift_message,
    }],
    "confidence": 0.9,
}, drift_message)):
    drift_response = client.post("/api/chat-complete", json={
        "conversation_id": drift_cid,
        "message": drift_message,
    })
assert drift_response.status_code == 200, drift_response.get_data(as_text=True)
drift_body = drift_response.get_json()
assert drift_body["state"]["intake"]["status"] == "collecting"
assert drift_body["state"]["intake"]["profile_updates"][0]["value"] == "泽龙"
assert "preferred_name" not in drift_body["state"]["ip_profile"]["facts"]
stored_drift = server.load_conversation(drift_cid)
assert stored_drift["title"] == "泽龙 · IP 诊断"
assert any(item["role"] == "user" and item["content"] == drift_message for item in stored_drift["messages"])

repair_cid = client.post("/api/conversations").get_json()["id"]
repair_message = "我在广州做 FDE"
invalid_repair_decision = {
    "decision": "propose_checkpoint",
    "checkpoint": 2,
    "reply": "我先整理一下。",
    "draft": "城市：广州；职业：FDE。",
    "self_review": "仍需本人确认。",
    "profile_updates": [],
    "confidence": 0.9,
}
valid_repair_decision = {
    "decision": "ask_follow_up",
    "checkpoint": 0,
    "reply": "收到。你希望我怎么称呼你？",
    "draft": "",
    "self_review": "",
    "profile_updates": [],
    "confidence": 0.9,
}
with patch.object(server, "_coach_model_decision", side_effect=[
    (invalid_repair_decision, repair_message),
    (valid_repair_decision, repair_message),
]) as repair_model:
    repair_response = client.post("/api/chat-complete", json={
        "conversation_id": repair_cid,
        "message": repair_message,
    })
assert repair_response.status_code == 200, repair_response.get_data(as_text=True)
assert repair_model.call_count == 2
assert "跨越当前断点" in repair_model.call_args_list[1].kwargs["repair_error"]
stored_repair = server.load_conversation(repair_cid)
assert sum(item["role"] == "user" and item["content"] == repair_message for item in stored_repair["messages"]) == 1

with patch.object(server, "_coach_model_decision", return_value=(valid_repair_decision, repair_message)) as conflict_model, \
        patch.object(server, "_persist_model_turn", side_effect=server.coach_harness.HarnessConflict("stale")):
    try:
        server._process_model_turn(repair_cid, repair_message)
        raise AssertionError("state conflict should propagate")
    except server.coach_harness.HarnessConflict:
        pass
assert conflict_model.call_count == 1

continuation_cid = client.post("/api/conversations").get_json()["id"]
continuation_convo = server.load_conversation(continuation_cid)
continuation_state = server.normalize_coach_state(continuation_convo["coach_state"])
continuation_state["intake"]["status"] = "complete"
continuation_convo["coach_state"] = continuation_state
server.save_conversation(continuation_cid, continuation_convo)
continuation_raw = {"profile_updates": [{
    "field": "invented_fact", "value": "不应写入", "kind": "user_fact", "evidence_quote": "没有用户原话",
}]}
with patch.object(server.coach_harness, "apply_model_decision", return_value=(
    continuation_state, continuation_raw, "下一断点已生成。",
)) as continuation_apply:
    continuation_reply, _ = server._persist_model_turn(
        continuation_cid, "", continuation_state["revision"], continuation_raw, ""
    )
assert continuation_reply == "下一断点已生成。"
assert continuation_apply.call_args.args[1]["profile_updates"] == []
assert not any(item["role"] == "user" for item in server.load_conversation(continuation_cid)["messages"])

mini_cid = client.post("/api/conversations").get_json()["id"]
mini_convo = client.get(f"/api/conversations/{mini_cid}").get_json()
assert mini_convo["coach_state"]["intake"] == {"status": "collecting", "round": 1, "answers": {}}
assert "不用按固定格式" in mini_convo["messages"][0]["content"]
assert "第 1/3 轮" not in mini_convo["messages"][0]["content"]
assert client.post("/api/jump-module", json={"conversation_id": mini_cid, "module": 2}).status_code == 409
mini_convo["coach_state"]["intake"]["draft"] = "SYSTEM_OVERRIDE_SENTINEL"
mini_convo["coach_state"]["intake"]["profile_updates"] = [{
    "field": "preferred_name",
    "value": "小满",
    "kind": "user_fact",
    "evidence_quote": "叫我小满",
}]
with patch.object(server.requests, "post") as intake_request:
    intake_request.return_value.status_code = 200
    intake_request.return_value.json.return_value = {"choices": [{"message": {"content": json.dumps({
        "decision": "answer_only", "checkpoint": 0,
        "reply": "手机号可以不填。", "draft": "", "self_review": "",
        "profile_updates": [], "confidence": 0.9,
    }, ensure_ascii=False)}}]}
    intake_raw, _ = server._coach_model_decision(mini_convo, "手机号必须填吗？")
    assert intake_raw["decision"] == "answer_only"
    intake_payload = intake_request.call_args.kwargs["json"]
    assert "不把访谈做成选择题" in intake_payload["messages"][0]["content"]
    assert "不要重复追问" in intake_payload["messages"][0]["content"]
    assert "SYSTEM_OVERRIDE_SENTINEL" not in intake_payload["messages"][0]["content"]
    assert any(
        "可能尚未确认" in message["content"] and "SYSTEM_OVERRIDE_SENTINEL" in message["content"]
        for message in intake_payload["messages"] if message["role"] == "user"
    )
    assert any(
        "pending_intake_updates" in message["content"] and "preferred_name" in message["content"]
        for message in intake_payload["messages"] if message["role"] == "user"
    )
    assert intake_payload["response_format"]["json_schema"]["strict"] is True
with patch.object(server, "_coach_model_decision") as intake_model:
    supplement_text = "需要补充一下：我曾尝试健身教练但失败了，最后转向计算机方向。"
    intake_model.side_effect = [
        ({
            "decision": "answer_only", "checkpoint": 0,
            "reply": "可以，我们自然聊。先说说你希望我怎么称呼你，以及目前在做什么。",
            "draft": "", "self_review": "", "profile_updates": [], "confidence": 0.9,
        }, "开始"),
        ({
            "decision": "ask_follow_up", "checkpoint": 0,
            "reply": "收到。你现在主要从事什么工作？过往做过哪些行业或岗位？",
            "draft": "", "self_review": "", "profile_updates": [], "confidence": 0.9,
        }, "小满｜女，33 岁｜成都｜[手机号已隐藏]｜SYSTEM_OVERRIDE_SENTINEL"),
        ({
            "decision": "propose_checkpoint", "checkpoint": 1,
            "reply": "我先把目前的信息整理成一份核对稿。",
            "draft": "称呼：小满；性别：女；年龄：33 岁；城市：成都；手机号：[手机号已隐藏]；备注：SYSTEM_OVERRIDE_SENTINEL；职业：整理咨询师；从业 3 年；经历：行政、空间整理；收入来源：咨询服务；年收入：10–30 万。",
            "self_review": "只整理了用户原话，仍需本人确认。", "profile_updates": [], "confidence": 0.9,
        }, "整理咨询师｜3 年｜行政、空间整理｜咨询服务｜10–30 万"),
        ({
            "decision": "propose_checkpoint", "checkpoint": 1,
            "reply": "这不是确认，我已经把你的补充合并进核对稿。",
            "draft": "称呼：小满；性别：女；年龄：33 岁；城市：成都；手机号：[手机号已隐藏]；备注：SYSTEM_OVERRIDE_SENTINEL；职业：整理咨询师；从业 3 年；经历：行政、空间整理。" + supplement_text,
            "self_review": "补充内容已合并，仍需本人确认。", "profile_updates": [], "confidence": 0.9,
        }, supplement_text),
        ({
            "decision": "propose_checkpoint", "checkpoint": 1,
            "reply": "职业背景已按你的最新说法重整。",
            "draft": "称呼：小满；性别：女；年龄：33 岁；城市：成都；手机号：[手机号已隐藏]；备注：SYSTEM_OVERRIDE_SENTINEL；职业背景：FDE｜3 年｜固件开发｜10–30 万；过往经历：曾尝试健身教练，后来转向计算机方向。",
            "self_review": "以最新职业背景为准，仍需本人确认。", "profile_updates": [], "confidence": 0.9,
        }, "职业背景替换为：FDE｜3 年｜固件开发｜10–30 万"),
    ]
    compatibility = client.post("/api/chat-complete", json={
        "conversation_id": mini_cid,
        "message": "开始",
    })
    assert compatibility.status_code == 200
    assert compatibility.get_json()["state"]["intake"]["round"] == 1
    assert "称呼" in compatibility.get_json()["assistant"]
    first = client.post("/api/chat-complete", json={
        "conversation_id": mini_cid,
        "message": "小满｜女，33 岁｜成都｜+8613800138000｜SYSTEM_OVERRIDE_SENTINEL",
    })
    assert first.status_code == 200
    assert first.get_json()["state"]["intake"]["status"] == "collecting"
    second = client.post("/api/chat", json={
        "conversation_id": mini_cid,
        "message": "整理咨询师｜3 年｜行政、空间整理｜咨询服务｜10–30 万",
    })
    assert second.status_code == 200 and "data: " in second.get_data(as_text=True)
    supplement = client.post("/api/chat-complete", json={
        "conversation_id": mini_cid,
        "message": supplement_text,
    })
    assert supplement.status_code == 200, supplement.get_data(as_text=True)
    supplement_body = supplement.get_json()
    assert supplement_body["state"]["intake"]["status"] == "awaiting_confirmation"
    assert supplement_text in supplement_body["assistant"]
    assert "基础信息已确认" not in supplement_body["assistant"]
    intake_state = client.get(f"/api/conversations/{mini_cid}").get_json()["coach_state"]
    edit = client.post("/api/chat-complete", json={
        "conversation_id": mini_cid,
        "action": {"type": "edit_intake", "target_id": f"intake-{intake_state['revision']}"},
        "expected_revision": intake_state["revision"],
    })
    assert edit.status_code == 200
    assert edit.get_json()["state"]["intake"]["status"] == "editing"
    corrected = client.post("/api/chat-complete", json={
        "conversation_id": mini_cid,
        "message": "职业背景替换为：FDE｜3 年｜固件开发｜10–30 万",
    })
    assert corrected.status_code == 200
    corrected_state = corrected.get_json()["state"]
    assert corrected_state["intake"]["status"] == "awaiting_confirmation"
    confirm_body = {
        "conversation_id": mini_cid,
        "action": {"type": "confirm_intake", "target_id": f"intake-{corrected_state['revision']}"},
        "expected_revision": corrected_state["revision"],
        "request_id": "confirm-intake-runtime-1",
    }
    third = client.post("/api/chat-complete", json=confirm_body)
    assert third.status_code == 200, third.get_data(as_text=True)
    assert third.get_json()["state"]["intake"]["status"] == "complete"
    assert "正式进入模块 1" in third.get_json()["assistant"]
    replay = client.post("/api/chat-complete", json=confirm_body)
    assert replay.status_code == 200 and replay.get_json()["replayed"] is True
    assert replay.get_json()["state"]["revision"] == third.get_json()["state"]["revision"]
    assert intake_model.call_count == 5
stored_intake = server.load_conversation(mini_cid)
stored_text = json.dumps(stored_intake, ensure_ascii=False)
assert "13800138000" not in stored_text and "[手机号已隐藏]" in stored_text
assert "13800138000" not in json.dumps(server._foundation_source_messages(stored_intake), ensure_ascii=False)
assert "13800138000" not in server.build_system_prompt(mini_cid)
assert server._redact_mobile_numbers("+8613800138000 / 008613800138000") == "[手机号已隐藏] / [手机号已隐藏]"
assert "SYSTEM_OVERRIDE_SENTINEL" not in server.build_system_prompt(mini_cid)
assert not server._intake_pending({"current_module": 1})
post_confirm_supplement = "需要补充一下：我以前还做过健身教练，但是失败了，后来才转向计算机方向。"
with patch.object(server, "_coach_model_decision", return_value=({
    "decision": "revise_intake",
    "checkpoint": 0,
    "reply": "我把这段经历补进基础资料，先请你重新核对。",
    "draft": "称呼：小满；职业：FDE；过往经历：曾做健身教练，后来转向计算机方向。",
    "self_review": "保留原资料并加入本轮补充，仍需本人确认。",
    "profile_updates": [{
        "field": "previous_career",
        "value": "曾做健身教练，后来转向计算机方向",
        "kind": "user_fact",
        "evidence_quote": "我以前还做过健身教练",
    }],
    "confidence": 0.95,
}, post_confirm_supplement)):
    reopened = client.post("/api/chat-complete", json={
        "conversation_id": mini_cid,
        "message": post_confirm_supplement,
    })
assert reopened.status_code == 200, reopened.get_data(as_text=True)
reopened_state = reopened.get_json()["state"]
assert reopened_state["intake"]["status"] == "awaiting_confirmation"
assert reopened_state["intake"]["mode"] == "revision"
assert reopened_state["current_module"] == 1 and reopened_state["module_step"] == 0
assert "基础信息已确认" not in reopened.get_json()["assistant"]
assert "当前模块不会自动推进" in reopened.get_json()["assistant"]
reconfirmed = client.post("/api/chat-complete", json={
    "conversation_id": mini_cid,
    "action": {"type": "confirm_intake", "target_id": f"intake-{reopened_state['revision']}"},
    "expected_revision": reopened_state["revision"],
    "request_id": "confirm-intake-revision-runtime-1",
})
assert reconfirmed.status_code == 200, reconfirmed.get_data(as_text=True)
assert reconfirmed.get_json()["state"]["intake"]["status"] == "complete"
assert reconfirmed.get_json()["state"]["current_module"] == 1
assert reconfirmed.get_json()["state"]["module_step"] == 0
assert "基础信息补充已确认" in reconfirmed.get_json()["assistant"]
assert reconfirmed.get_json()["state"]["ip_profile"]["facts"]["previous_career"]["value"].startswith("曾做健身教练")
module_turn_message = """其实最大的转折点在于，我之前也做过类似的底层工作，都是用汗水换金钱。我做过很多散工，比如当服务员、修车，也经常打螺丝，职业方向一直比较零散。

直到转入 AI 行业之后，我开始做出新的尝试，帮助他们去搭建 Agent，把 AI 用到真实的业务当中去。虽然只有三个月，但这一次转向，可能让我找到了一个更愿意长期投入的方向。"""
with patch.object(server, "_coach_model_decision", return_value=({
    "decision": "ask_follow_up",
    "checkpoint": 0,
    "reply": "这次转向之后，哪一次真实结果最让你确认自己愿意长期投入？",
    "draft": "",
    "self_review": "",
    "profile_updates": [{
        "field": "turning_point",
        "value": "从底层散工转向 AI 行业并开始帮助企业搭建 Agent",
        "kind": "user_fact",
        "evidence_quote": "直到转入 AI 行业之后",
    }],
    "confidence": 0.9,
}, module_turn_message)):
    module_turn = client.post("/api/chat-complete", json={
        "conversation_id": mini_cid,
        "message": module_turn_message,
    })
assert module_turn.status_code == 200, module_turn.get_data(as_text=True)
assert module_turn.get_json()["state"]["pending"]["status"] == "collecting"
assert module_turn.get_json()["actions"] == []
stored_module_turn = server.load_conversation(mini_cid)
assert sum(item["role"] == "user" and item["content"] == module_turn_message for item in stored_module_turn["messages"]) == 1
stored_intake = server.load_conversation(mini_cid)
stored_intake["messages"].extend(
    {"role": "assistant", "content": f"历史消息 {index}"} for index in range(45)
)
server.save_conversation(mini_cid, stored_intake)
with patch.object(server.requests, "post") as chat_model:
    chat_model.return_value.status_code = 200
    chat_model.return_value.json.return_value = {"choices": [{"message": {"content": json.dumps({
        "decision": "ask_follow_up",
        "checkpoint": 0,
        "reply": "请讲一段对你影响最大的关键经历。",
        "draft": "",
        "self_review": "",
        "profile_updates": [],
        "confidence": 0.8,
    }, ensure_ascii=False)}}]}
    module_reply = client.post(
        "/api/chat-complete", json={"conversation_id": mini_cid, "message": "我曾经重新选择职业方向。"}
    )
    assert module_reply.status_code == 200, module_reply.get_data(as_text=True)
    chat_model.assert_called_once()
    model_payload = chat_model.call_args.kwargs["json"]
    assert model_payload["stream"] is False
    assert model_payload["response_format"]["json_schema"]["strict"] is True
    assert "revise_intake" in model_payload["response_format"]["json_schema"]["schema"]["properties"]["decision"]["enum"]
    model_messages = model_payload["messages"]
    assert "decision=revise_intake" in model_messages[0]["content"]
    assert "SYSTEM_OVERRIDE_SENTINEL" not in model_messages[0]["content"]
    intake_contexts = [message for message in model_messages if message["role"] == "user" and "此前确认的基础资料" in message["content"]]
    assert len(intake_contexts) == 1 and "SYSTEM_OVERRIDE_SENTINEL" in intake_contexts[0]["content"]
    assert "pending_module_updates" in intake_contexts[0]["content"]
    assert "turning_point" in intake_contexts[0]["content"]
    assert "13800138000" not in json.dumps(model_messages, ensure_ascii=False)
server.current_account_id = lambda: "acct_b"
assert client.post(
    "/api/chat-complete", json={"conversation_id": mini_cid, "message": "越权"}
).status_code == 404
server.current_account_id = lambda: "acct_a"

uploaded = client.post(
    "/api/agnes/upload-image",
    data={"files": (io.BytesIO(b"test-image"), "test.png")},
    content_type="multipart/form-data",
    base_url="https://huangquechuanmei.com",
    headers={
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Prefix": "/workbench/ip12",
    },
).get_json()
assert uploaded["files"][0]["public_url"].startswith(
    "https://huangquechuanmei.com/workbench/ip12/media/agnes/images/"
), uploaded

# Active internal-tool directories count toward quota. A successful upload must
# increase used space, and the next upload must be rejected on cumulative use.
original_quota = artifact_store.DATA_QUOTA_BYTES
agnes_images = Path(os.environ["HERMES_DATA_DIR"]) / "agnes_lab" / "images"
agnes_before_files = set(agnes_images.iterdir())
agnes_before_bytes = artifact_store.directory_size()
artifact_store.DATA_QUOTA_BYTES = agnes_before_bytes + 4096
agnes_first = client.post(
    "/api/agnes/upload-image",
    data={"files": (io.BytesIO(b"first-agnes"), "first.png")},
    content_type="multipart/form-data",
)
assert agnes_first.status_code == 200, agnes_first.get_data(as_text=True)
agnes_after_bytes = artifact_store.directory_size()
assert agnes_after_bytes > agnes_before_bytes
agnes_after_files = set(agnes_images.iterdir())
assert len(agnes_after_files - agnes_before_files) == 1
artifact_store.DATA_QUOTA_BYTES = agnes_after_bytes + 1
agnes_second = client.post(
    "/api/agnes/upload-image",
    data={"files": (io.BytesIO(b"second-agnes"), "second.png")},
    content_type="multipart/form-data",
)
assert agnes_second.status_code == 507, agnes_second.get_data(as_text=True)
assert set(agnes_images.iterdir()) == agnes_after_files

team_uploads = (
    Path(os.environ["HERMES_DATA_DIR"]) / "team_workbench" / "uploads" / "images"
)
team_before_files = set(team_uploads.iterdir())
team_before_bytes = artifact_store.directory_size()
artifact_store.DATA_QUOTA_BYTES = team_before_bytes + 4096
team_first = client.post(
    "/api/team-workbench/upload",
    data={"files": (io.BytesIO(b"first-team"), "first.png")},
    content_type="multipart/form-data",
)
assert team_first.status_code == 200, team_first.get_data(as_text=True)
team_after_bytes = artifact_store.directory_size()
assert team_after_bytes > team_before_bytes
team_after_files = set(team_uploads.iterdir())
assert len(team_after_files - team_before_files) == 1
artifact_store.DATA_QUOTA_BYTES = team_after_bytes + 1
team_second = client.post(
    "/api/team-workbench/upload",
    data={"files": (io.BytesIO(b"second-team"), "second.png")},
    content_type="multipart/form-data",
)
assert team_second.status_code == 507, team_second.get_data(as_text=True)
assert set(team_uploads.iterdir()) == team_after_files
assert not list(Path(os.environ["HERMES_DATA_DIR"]).rglob("*.tmp"))
artifact_store.DATA_QUOTA_BYTES = original_quota

media = client.post(
    "/api/media/upload",
    json={
        "keyword": "../../outside",
        "filename": "../../probe.png",
        "data": base64.b64encode(b"image").decode(),
    },
)
assert media.status_code == 200, media.get_data(as_text=True)
media_root = Path(os.environ["HERMES_DATA_DIR"]).resolve()
index = json.loads((media_root / "media_library" / "index.json").read_text())
saved = Path(index["entries"][media.get_json()["id"]]["file_path"]).resolve()
assert saved.is_relative_to(media_root)
assert all(Path(entry["file_path"]).resolve().is_relative_to(media_root)
           for entry in index["entries"].values())
assert all(entry["owner_username"] == "admin" for entry in index["entries"].values())
assert client.get(
    "/api/media/search?q=outside",
    headers={"Authorization": "Bearer member-b-token"},
).get_json()["results"] == []

# Same-second/same-keyword uploads must retain both owners without keyword leakage.
source_a = media_root / "same-a.bin"
source_b = media_root / "same-b.bin"
source_a.write_bytes(b"owner-a")
source_b.write_bytes(b"owner-b")
with patch.object(media_library.time, "time", return_value=1234567890):
    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(
            media_library.MediaLibrary.add,
            "private-campaign", str(source_a),
            owner_username="member-a",
        )
        future_b = pool.submit(
            media_library.MediaLibrary.add,
            "private-campaign", str(source_b),
            owner_username="member-b",
        )
        media_id_a, media_id_b = future_a.result(), future_b.result()
assert media_id_a != media_id_b
index = json.loads((media_root / "media_library" / "index.json").read_text())
assert index["entries"][media_id_a]["owner_username"] == "member-a"
assert index["entries"][media_id_b]["owner_username"] == "member-b"
assert media_library.MediaLibrary.search(
    "private-campaign", owner_username="member-a"
) == [index["entries"][media_id_a]]
assert media_library.MediaLibrary.search(
    "private-campaign", owner_username="member-b"
) == [index["entries"][media_id_b]]
admin_stats = media_library.MediaLibrary.stats(owner_username="admin")
member_a_stats = media_library.MediaLibrary.stats(owner_username="member-a")
assert "private-campaign" not in admin_stats["keywords"]
assert member_a_stats["total_files"] == 1
assert member_a_stats["keywords"] == ["private-campaign"]

# The same transaction lock must also protect independent worker processes.
process_sources = []
processes = []
child_code = (
    "import sys;"
    "from media_library import MediaLibrary;"
    "MediaLibrary.add(sys.argv[1],sys.argv[2],owner_username=sys.argv[3])"
)
for i in range(4):
    source = media_root / f"process-{i}.bin"
    source.write_bytes(f"process-{i}".encode())
    process_sources.append(source)
    processes.append(subprocess.Popen(
        [sys.executable, "-c", child_code, "process-private", str(source), f"process-{i}"],
        cwd=os.getcwd(),
        env=os.environ.copy(),
    ))
assert all(process.wait(timeout=30) == 0 for process in processes)
index = json.loads((media_root / "media_library" / "index.json").read_text())
assert sum(
    entry.get("keyword") == "process-private"
    for entry in index["entries"].values()
) == 4

assert client.post(
    "/api/media/upload",
    json={"filename": "../../probe.py", "data": base64.b64encode(b"bad").decode()},
).status_code == 400

pipeline_upload = client.post(
    "/api/pipeline-upload",
    data={"video": (io.BytesIO(b"video"), "../../clip.mp4")},
    content_type="multipart/form-data",
)
assert pipeline_upload.status_code == 200, pipeline_upload.get_data(as_text=True)
pipeline_upload_id = pipeline_upload.get_json()["upload_id"]
pipeline_path = artifact_store.find_upload("admin", pipeline_upload_id)
assert pipeline_path.is_relative_to((Path(os.environ["HERMES_DATA_DIR"]) / "users").resolve())
assert client.post(
    "/api/pipeline",
    json={"upload_id": pipeline_upload_id, "topic": "test"},
    headers={"Authorization": "Bearer member-b-token"},
).status_code == 400
assert client.post(
    "/api/pipeline-upload",
    data={"video": (io.BytesIO(b"bad"), "../../clip.py")},
    content_type="multipart/form-data",
).status_code == 400
assert client.post(
    "/api/pipeline", json={"video_path": "/etc/passwd", "topic": "test"}
).status_code == 400

def fake_video_file(work_dir, name="output.mp4"):
    path = Path(work_dir) / name
    path.write_bytes(b"generated-video")
    return str(path)

def assert_owned_video(response):
    assert response.status_code == 200, response.get_data(as_text=True)
    url = response.get_json()["video_url"]
    owner_response = client.get(url)
    assert owner_response.status_code == 200, (url, owner_response.status_code)
    other_response = client.get(
        url, headers={"Authorization": "Bearer member-b-token"}
    )
    assert other_response.status_code == 404, (url, other_response.status_code)

fake_script = {
    "title": "test", "narration_full": "test",
    "scenes": [{"narration": "test", "visual": "test"}],
}
with patch.object(video_factory, "generate_script", return_value=fake_script), \
     patch.object(video_factory, "generate_all_images", side_effect=lambda scenes, work_dir: scenes), \
     patch.object(video_factory, "generate_tts_pro", return_value="audio"), \
     patch.object(video_factory, "generate_subtitles", return_value="subtitle"), \
     patch.object(video_factory, "compose_video_pro", side_effect=lambda *args: fake_video_file(args[-1])):
    assert_owned_video(client.post("/api/generate-video", json={"topic": "test"}))

analysis_id, analysis_root = artifact_store.analysis_dir("admin")
analysis_root.mkdir(parents=True, exist_ok=True)
(analysis_root / "result.json").write_text(json.dumps({
    "analysis_id": analysis_id, "owner_username": "admin",
    "analysis": "analysis", "transcript": "transcript",
}))
assert client.post(
    "/api/generate-from-analysis",
    json={"analysis_id": analysis_id, "topic": "test"},
    headers={"Authorization": "Bearer member-b-token"},
).status_code == 404
with patch.object(video_analyzer, "generate_script_with_reference", return_value=fake_script), \
     patch.object(video_factory, "generate_all_images", side_effect=lambda scenes, work_dir: scenes), \
     patch.object(video_factory, "generate_tts_pro", return_value="audio"), \
     patch.object(video_factory, "generate_subtitles", return_value="subtitle"), \
     patch.object(video_factory, "compose_video_pro", side_effect=lambda *args: fake_video_file(args[-1])):
    assert_owned_video(client.post(
        "/api/generate-from-analysis",
        json={"analysis_id": analysis_id, "topic": "test"},
    ))

with patch.object(video_pipeline, "transcribe_video", return_value={"full_text": "test"}), \
     patch.object(video_pipeline, "optimize", return_value={
         "title": "test", "scenes": [], "narration_full": "test"
     }), \
     patch.object(video_pipeline, "generate_videos", return_value=[]), \
     patch.object(video_pipeline, "generate_tts", return_value="audio"), \
     patch.object(video_pipeline, "compose_video", side_effect=lambda *args: fake_video_file(args[-1])), \
     patch.object(video_vision, "analyze_video_visual", return_value={"frames_analyzed": 1}), \
     patch.object(video_pipeline.KnowledgeBase, "add_formula"):
    assert_owned_video(client.post(
        "/api/pipeline",
        json={"upload_id": pipeline_upload_id, "topic": "test"},
    ))

with patch.object(video_replica, "replicate", return_value=[]), \
     patch.object(video_replica, "compose_final", side_effect=lambda clips, text, work_dir: fake_video_file(work_dir)):
    assert_owned_video(client.post(
        "/api/replica",
        json={"topic": "test", "segments": [{"text": "test"}]},
    ))

with patch.object(image_services.http_requests, "get") as proxy_get:
    blocked = client.get(
        "/api/proxy-image",
        query_string={"url": "http://127.0.0.1:3102/?pollinations"},
    )
    assert blocked.status_code == 400
    proxy_get.assert_not_called()

with patch.object(video_analyzer, "download_video") as video_download:
    blocked = client.post("/api/analyze-video", json={"url": "http://127.0.0.1/test"})
    assert blocked.status_code == 400
    video_download.assert_not_called()
assert client.post(
    "/api/analyze-video", json={"url": "https://example.com/video"}
).status_code == 400

# Low quota must reject before any downloader side effect or analysis directory.
original_analysis_limit = video_analyzer.ANALYSIS_MAX_DOWNLOAD_BYTES
original_quota = artifact_store.DATA_QUOTA_BYTES
video_analyzer.ANALYSIS_MAX_DOWNLOAD_BYTES = 400
analyses_root = artifact_store.user_dir("admin", "analyses")
before_analyses = {path.name for path in analyses_root.iterdir()}
artifact_store.DATA_QUOTA_BYTES = artifact_store.directory_size() + 399
with patch.object(video_analyzer, "is_public_video_url", return_value=True), \
     patch.object(video_analyzer, "download_video") as quota_download:
    quota_response = client.post(
        "/api/analyze-video",
        json={"url": "https://douyin.com/video/quota"},
    )
assert quota_response.status_code == 507, quota_response.get_data(as_text=True)
quota_download.assert_not_called()
assert {path.name for path in analyses_root.iterdir()} == before_analyses

# Two analyses competing for the final quota cannot both reserve capacity.
analysis_started = threading.Event()
analysis_release = threading.Event()
artifact_store.DATA_QUOTA_BYTES = artifact_store.directory_size() + 700
before_analyses = {path.name for path in analyses_root.iterdir()}

def fake_analysis_download(url, work_dir):
    analysis_started.set()
    assert analysis_release.wait(timeout=10)
    path = Path(work_dir) / "source.mp4"
    path.write_bytes(b"analysis-video")
    return str(path)

def run_analysis():
    thread_client = app.test_client()
    return thread_client.post(
        "/api/analyze-video",
        json={"url": "https://douyin.com/video/test"},
        headers={"Authorization": "Bearer admin-token"},
    )

analysis_exdev = []
def replace_analysis_cross_device(source, destination):
    source_path = Path(source)
    destination_path = Path(destination)
    if (
        not analysis_exdev
        and source_path.name == "source.mp4"
        and source_path.parent.name.startswith("hermes-analysis-")
        and destination_path.name == "source.mp4"
    ):
        analysis_exdev.append((source_path, destination_path))
        raise OSError(errno.EXDEV, "cross-device link")
    return real_replace(source, destination)

with patch.object(security, "_audit"), \
     patch.object(video_analyzer, "is_public_video_url", return_value=True), \
     patch.object(video_analyzer, "download_video", side_effect=fake_analysis_download), \
     patch.object(video_analyzer, "transcribe_video", return_value="transcript"), \
     patch.object(video_analyzer, "analyze_transcript", return_value="analysis"), \
     patch.object(
         artifact_store.os, "replace", side_effect=replace_analysis_cross_device
     ):
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(run_analysis)
        assert analysis_started.wait(timeout=10)
        second = run_analysis()
        assert second.status_code == 507, second.get_data(as_text=True)
        analysis_release.set()
        first = first_future.result(timeout=10)
assert first.status_code == 200, first.get_data(as_text=True)
assert len(analysis_exdev) == 1
after_analyses = {path.name for path in analyses_root.iterdir()}
assert len(after_analyses - before_analyses) == 1
assert artifact_store.directory_size() <= artifact_store.DATA_QUOTA_BYTES, (
    artifact_store.directory_size(), artifact_store.DATA_QUOTA_BYTES
)
assert json.loads(artifact_store.RESERVATIONS_FILE.read_text()) == {}
video_analyzer.ANALYSIS_MAX_DOWNLOAD_BYTES = original_analysis_limit
artifact_store.DATA_QUOTA_BYTES = original_quota

class EmptyPexelsResponse:
    status_code = 200
    text = ""
    def json(self):
        return {"photos": []}

with patch.object(media_library.MediaLibrary, "_owner", return_value="admin"), \
     patch.object(media_library.MediaLibrary, "search", return_value=[]), \
     patch.object(media_library.KnowledgeBase, "get_keyword_map", return_value=None), \
     patch.object(media_library, "google_search_images", return_value=[]), \
     patch("requests.get", return_value=EmptyPexelsResponse()) as pexels_get:
    assert media_library.get_best_image("test") == {"source": "none", "keyword": "test"}
    assert pexels_get.call_args.kwargs["headers"]["Authorization"] == "pexels-dummy"

class ImageResponse:
    status_code = 200
    text = ""
    content = b"image-bytes" * 600
    def __init__(self, payload=None):
        self.payload = payload or {}
    def json(self):
        return self.payload

pexels_response = ImageResponse({
    "photos": [{
        "src": {"large": "https://img.example/pexels.jpg"},
        "photographer": "Pexels Owner",
    }],
})
with patch.object(media_library.MediaLibrary, "_owner", return_value="admin"), \
     patch.object(media_library.MediaLibrary, "search", return_value=[]), \
     patch.object(media_library.KnowledgeBase, "get_keyword_map", return_value=None), \
     patch("requests.get", side_effect=[pexels_response, ImageResponse()]):
    result = media_library.get_best_image("pexels-owned")
assert result["source"] == "pexels", result
pexels_entries = media_library.MediaLibrary._load()["entries"].values()
pexels_entry = next(entry for entry in pexels_entries if entry["keyword"] == "pexels-owned")
assert pexels_entry["owner_username"] == "admin", pexels_entry
assert Path(pexels_entry["file_path"]).is_relative_to(
    artifact_store.user_dir("admin", "media")
), pexels_entry

with patch.object(media_library.MediaLibrary, "_owner", return_value="admin"), \
     patch.object(media_library.MediaLibrary, "search", return_value=[]), \
     patch.object(media_library.KnowledgeBase, "get_keyword_map", return_value=None), \
     patch.object(media_library, "PEXELS_KEY", ""), \
     patch.object(media_library, "google_search_images", return_value=[{
         "url": "https://img.example/google.jpg",
         "title": "Google Owner",
     }]), \
     patch("requests.get", return_value=ImageResponse()):
    result = media_library.get_best_image("google-owned")
assert result["source"] == "google", result
google_entries = media_library.MediaLibrary._load()["entries"].values()
google_entry = next(entry for entry in google_entries if entry["keyword"] == "google-owned")
assert google_entry["owner_username"] == "admin", google_entry
assert Path(google_entry["file_path"]).is_relative_to(
    artifact_store.user_dir("admin", "media")
), google_entry

with patch.object(video_replica, "PEXELS_KEY", ""), \
     patch("requests.get") as no_key_get:
    assert video_replica.search_pexels("test") is None
    no_key_get.assert_not_called()
with patch.object(video_replica, "PEXELS_KEY", "pexels-dummy"), \
     patch("requests.get", return_value=EmptyPexelsResponse()) as video_pexels_get:
    assert video_replica.search_pexels("test") is None
    assert video_pexels_get.call_args.kwargs["headers"]["Authorization"] == "pexels-dummy"
print("HERMES_RUNTIME_OK")
'''
        with tempfile.TemporaryDirectory() as data_dir:
            env = os.environ.copy()
            env.update(
                OPENAI_API_KEY="dummy",
                HERMES_HOME=data_dir,
                HERMES_DATA_DIR=data_dir,
                HERMES_ENABLE_INTERNAL_TOOLS="1",
                PEXELS_API_KEY="pexels-dummy",
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=HERMES,
                env=env,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("HERMES_RUNTIME_OK", result.stdout)
if __name__ == "__main__":
    unittest.main()
