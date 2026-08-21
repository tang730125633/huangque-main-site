import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
HERMES = ROOT / "server" / "hermes_ip12"


class IP12ChoiceE2ETests(unittest.TestCase):
    def test_real_page_choice_keyboard_refresh_security_and_viewports(self):
        script = r'''
import threading
import shutil
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

import server
import security

server.current_account_id = lambda: "acct_e2e"
security._validate_token = lambda token: {"account_id": "acct_e2e", "username": "e2e", "role": "member"}
security.RATE_REQUESTS = 100

def choice_items():
    return [
        {"choice_id": "choice-1", "display_index": 1, "title": "<img onerror=x>", "summary": "<script>window.x=1</script>", "reason": "引号\"与😀", "caution": "[链接](https://x)", "recommended": False},
        {"choice_id": "choice-2", "display_index": 2, "title": "AI 工具实践型", "summary": "结合经验与工具实践", "reason": "识别度集中", "caution": "避免硬推销", "recommended": True},
        {"choice_id": "choice-3", "display_index": 3, "title": "长期成长记录型", "summary": "用真实经历连接用户", "reason": "连接感更强", "caution": "注意隐私", "recommended": False},
    ]

def model_decision(snapshot, _message, repair_error="", timeout_seconds=180):
    state = server.normalize_coach_state(snapshot["coach_state"])
    checkpoint = state["module_step"] + 1
    if server.coach_harness.is_choice_checkpoint(state["current_module"], checkpoint):
        return {
            "decision": "propose_checkpoint", "checkpoint": checkpoint,
            "reply": "请选择最适合你的方向。", "draft": "",
            "self_review": "只使用已确认资料。", "profile_updates": [],
            "choices": [{key: value for key, value in item.items() if key not in {"choice_id", "display_index"}} for item in choice_items()],
            "confidence": 0.9,
        }, "用户原话"
    return {
        "decision": "propose_checkpoint", "checkpoint": checkpoint,
        "reply": "这是下一模块关键词。", "draft": "关键词：真实、清晰、可靠",
        "self_review": "只使用已确认资料。", "profile_updates": [], "choices": [],
        "confidence": 0.9,
    }, "用户原话"

server._coach_model_decision = model_decision

def create_choice_project(cid):
    state = server.coach_harness.initial_state()
    state["revision"] = 5
    state["intake"] = {"status": "complete", "round": 3, "answers": {}}
    state["module_step"] = 1
    state["ip_profile"]["confirmed_outputs"]["1-1"] = {"content": "关键词：真实、行动、AI"}
    state["pending"] = {
        "id": cid + "-target", "kind": "checkpoint", "status": "awaiting_confirmation",
        "module": 1, "step": 2, "draft": "", "self_review": "已校验",
        "profile_updates": [], "confidence": 0.9,
        "choices": choice_items(),
    }
    message = server._assistant_message(
        "现在选择你最想呈现的方向。", "diagnostic_choice",
        prompt_version="diagnostic-choice-v1", model="test-model",
        choice_target_id=cid + "-target",
    )
    server.save_conversation(cid, {
        "id": cid, "title": "E2E Choice", "owner_account_id": "acct_e2e",
        "messages": [message], "coach_state": state,
        "reports": {}, "deliverables": {}, "updated": "",
    })

create_choice_project("e2e-click")
create_choice_project("e2e-keyboard")
create_choice_project("e2e-space")
create_choice_project("e2e-input-one")
create_choice_project("e2e-input-voice")
create_choice_project("e2e-edit")
create_choice_project("e2e-edit-stale")
create_choice_project("e2e-stale")
create_choice_project("e2e-recover")

generation = server.coach_harness.initial_state()
generation["revision"] = 5
generation["intake"] = {"status": "complete", "round": 3, "answers": {}}
generation["module_step"] = 1
generation["ip_profile"]["confirmed_outputs"]["1-1"] = {"content": "关键词：真实、行动、AI"}
server.save_conversation("e2e-migration", {
    "id": "e2e-migration", "title": "E2E Migration", "owner_account_id": "acct_e2e",
    "messages": [{"role": "assistant", "content": "旧版回答"}],
    "coach_state": {**generation, "schema_version": 1},
    "reports": {}, "deliverables": {}, "updated": "",
})

httpd = make_server("127.0.0.1", 0, server.app)
thread = threading.Thread(target=httpd.serve_forever, daemon=True)
thread.start()
base = f"http://127.0.0.1:{httpd.server_port}"

try:
    http = requests.Session()
    http.trust_env = False
    health = http.get(base + "/healthz", timeout=5).json()
    assert health["agent_release"] == "ip12-a0.1"
    assert health["state_schema"] == 2
    assert health["release_sha"] == "e2e-release-sha"
    with sync_playwright() as playwright:
        executable = Path(playwright.chromium.executable_path)
        browser_executable = str(executable) if executable.exists() else next((
            shutil.which(name) for name in (
                "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"
            ) if shutil.which(name)
        ), None)
        assert browser_executable, "Chromium missing; run: python -m playwright install chromium"
        browser = playwright.chromium.launch(headless=True, executable_path=browser_executable)
        context = browser.new_context(
            viewport={"width": 390, "height": 844}, reduced_motion="reduce",
            extra_http_headers={"Authorization": "Bearer test-token"},
        )
        page = context.new_page()
        console_errors = []
        page_errors = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)

        page.goto(base + "/?conversation_id=e2e-click", wait_until="networkidle")
        page.locator(".choice-action").first.wait_for(state="visible")
        assert page.locator(".choice-action").count() == 3
        assert page.locator(".choice-action img, .choice-action script, .choice-action a").count() == 0
        assert page.locator(".choice-title").first.inner_text() == "<img onerror=x>"
        assert "<script>window.x=1</script>" in page.locator(".choice-summary").first.inner_text()
        assert page.evaluate("window.x === undefined")
        assert page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches")
        assert page.locator(".choice-action").first.evaluate("el => el.getBoundingClientRect().height") >= 44
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert page.locator(".choice-copy").evaluate_all("els => els.every(el => el.scrollWidth <= el.clientWidth)")
        page.reload(wait_until="networkidle")
        assert page.locator(".choice-action").count() == 3
        page.locator(".choice-action").nth(1).dblclick()
        page.locator(".choice-receipt").wait_for(state="visible")
        assert "已选择 2" in page.locator(".choice-receipt summary").inner_text()
        page.reload(wait_until="networkidle")
        assert page.locator(".choice-action").count() == 0
        assert page.locator(".choice-receipt").count() == 1

        for width in (1024, 1440):
            page.set_viewport_size({"width": width, "height": 900})
            page.goto(base + "/?conversation_id=e2e-keyboard", wait_until="networkidle")
            assert page.locator(".choice-action").count() == 3
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

        first = page.locator(".choice-action").first
        first.focus()
        assert first.evaluate("el => document.activeElement === el")
        first.press("Enter")
        page.locator(".choice-receipt").wait_for(state="visible")
        assert "已选择 1" in page.locator(".choice-receipt summary").inner_text()

        page.goto(base + "/?conversation_id=e2e-space", wait_until="networkidle")
        page.locator(".choice-action").nth(2).focus()
        page.locator(".choice-action").nth(2).press("Space")
        page.locator(".choice-receipt").wait_for(state="visible")
        assert "已选择 3" in page.locator(".choice-receipt summary").inner_text()

        page.goto(base + "/?conversation_id=e2e-input-one", wait_until="networkidle")
        page.locator("#userInput").fill("1")
        page.locator("#userInput").press("Enter")
        page.locator(".choice-receipt").wait_for(state="visible")
        assert "已选择 1" in page.locator(".choice-receipt summary").inner_text()

        page.goto(base + "/?conversation_id=e2e-input-voice", wait_until="networkidle")
        page.locator("#userInput").fill("我选第二个")
        page.locator("#userInput").press("Enter")
        page.locator(".choice-receipt").wait_for(state="visible")
        assert "已选择 2" in page.locator(".choice-receipt summary").inner_text()

        page.goto(base + "/?conversation_id=e2e-edit", wait_until="networkidle")
        page.locator(".choice-edit").click()
        page.locator(".choice-editing-context").wait_for(state="visible")
        assert page.locator(".choice-action").count() == 0
        assert "AI 工具实践型" in page.locator(".choice-editing-context").inner_text()
        page.locator("#userInput").fill("语气更温和")
        page.locator("#userInput").press("Enter")
        page.wait_for_function("document.querySelectorAll('.choice-action').length === 3")
        assert page.locator(".choice-action").count() == 3

        page.goto(base + "/?conversation_id=e2e-edit-stale", wait_until="networkidle")
        edit_stale = server.load_conversation("e2e-edit-stale")
        edit_stale["coach_state"]["revision"] += 1
        server.save_conversation("e2e-edit-stale", edit_stale)
        page.locator(".choice-edit").click()
        page.wait_for_function("document.activeElement?.classList.contains('choice-edit')")
        assert page.locator(".choice-status").get_attribute("role") == "alert"

        page.goto(base + "/?conversation_id=e2e-migration", wait_until="networkidle")
        assert "升级" in page.locator("#chatArea").inner_text()
        page.get_by_role("button", name="生成新的三个方案").click()
        page.wait_for_function("document.querySelectorAll('.choice-action').length === 3")
        assert page.locator(".choice-action").count() == 3

        page.goto(base + "/?conversation_id=e2e-stale", wait_until="networkidle")
        stale = server.load_conversation("e2e-stale")
        stale["coach_state"]["revision"] += 1
        server.save_conversation("e2e-stale", stale)
        page.locator(".choice-action").first.click()
        page.wait_for_function("document.activeElement?.classList.contains('choice-action')")
        assert page.locator(".choice-action").first.evaluate("el => document.activeElement === el")

        page.goto(base + "/?conversation_id=e2e-recover", wait_until="networkidle")
        recover_convo = server.load_conversation("e2e-recover")
        recover_action = server.coach_harness.available_actions(recover_convo["coach_state"])[0]
        recover_result, recover_status = server.process_chat_request({
            "conversation_id": "e2e-recover",
            "action": {"type": recover_action["type"], "target_id": recover_action["target_id"], "choice_id": recover_action["choice_id"]},
            "expected_revision": recover_convo["coach_state"]["revision"],
            "request_id": "e2e-recover-request",
        })
        assert recover_status == 200 and recover_result["ok"]
        page.evaluate("newTurnRequestId=()=> 'e2e-recover-request'")
        page.route("**/api/chat", lambda route: route.fulfill(status=500, content_type="application/json", body='{"error":"temporary"}'))
        page.locator(".choice-action").first.click()
        page.locator(".choice-receipt").wait_for(state="visible", timeout=10000)
        page.unroute("**/api/chat")
        assert page.locator("#userInput").evaluate("el => document.activeElement === el")
        unexpected_console = [message for message in console_errors if "status of 409" not in message and "status of 500" not in message]
        assert not page_errors, page_errors
        assert not unexpected_console, unexpected_console
        browser.close()

    saved = server.load_conversation("e2e-click")
    user_choices = [m["content"] for m in saved["messages"] if m.get("role") == "user" and m.get("content", "").startswith("我选择")]
    assert user_choices == ["我选择 2：AI 工具实践型"]
    assert all("agent_trace" in m for m in saved["messages"] if m.get("role") == "assistant")
    assert all(m["agent_trace"]["release_sha"] == "e2e-release-sha" for m in saved["messages"] if m.get("role") == "assistant")
    assert any(m.get("content") == "1" for m in server.load_conversation("e2e-input-one")["messages"])
    assert any(m.get("content") == "我选第二个" for m in server.load_conversation("e2e-input-voice")["messages"])
    print("IP12_CHOICE_E2E_OK")
finally:
    httpd.shutdown()
    thread.join(timeout=2)
'''
        with tempfile.TemporaryDirectory() as data_dir:
            env = os.environ.copy()
            env.update(
                OPENAI_API_KEY="dummy",
                HERMES_HOME=data_dir,
                HERMES_DATA_DIR=data_dir,
                IP12_RELEASE_SHA="e2e-release-sha",
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=HERMES,
                env=env,
                capture_output=True,
                text=True,
                timeout=90,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("IP12_CHOICE_E2E_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
