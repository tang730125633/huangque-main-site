"""Private customer-page QA journeys.

The first pilot intentionally supports one operation. Stable Playwright steps are
cheaper and easier to diagnose than an agent; a visual agent can become a fallback
when page drift is measured, without changing the evidence contract.
"""

import hashlib
import json
import pathlib
import tempfile
import time
import urllib.parse


class BrowserQAError(RuntimeError):
    """The journey definitely failed before or after a known submission."""


class BrowserSubmitUncertain(RuntimeError):
    """A customer submit may have reached production; never retry automatically."""


CHECK_NAMES = {
    "entry": "客户页面打开",
    "fixture": "预设素材加载",
    "submit": "客户按钮真实提交",
    "result": "作品在客户页可见",
    "download": "作品下载与解码",
    "correlation": "同一 job_id 关联生产证据",
}
PROMPT_EDITOR_SELECTOR = "#bPrompt + .hq-image-editor"


def validate_nb2_reference_payload(payload, prompt):
    """Reject drift before the paid request leaves the browser."""
    references = payload.get("reference_images") or []
    if payload.get("source_page") != "banana":
        raise BrowserQAError("客户页没有标记图片生成功能来源")
    if payload.get("model") != "nb2":
        raise BrowserQAError("客户页没有选择纳米香蕉 2")
    if payload.get("quality") != "std" or int(payload.get("count") or 0) != 1:
        raise BrowserQAError("客户页没有保持单张标准清晰度低成本参数")
    if str(payload.get("prompt") or "").strip() != str(prompt or "").strip():
        raise BrowserQAError("客户页提示词与预设测试包不一致")
    if len(references) != 1 or not isinstance(references[0], str) or len(references[0]) < 32:
        raise BrowserQAError("客户页没有加载唯一的私有参考图")


def _capture_step(page, key, detail):
    raw = page.screenshot(type="jpeg", quality=55, full_page=False)
    return {
        "key": key,
        "name": CHECK_NAMES[key],
        "state": "passed",
        "detail": str(detail or "")[:220],
        "checked_at": int(time.time()),
        "screenshot_sha256": hashlib.sha256(raw).hexdigest(),
    }


def _cookie(token, origin, cookie_name):
    parsed = urllib.parse.urlparse(origin)
    if parsed.scheme != "https" or parsed.hostname not in {
        "huangquechuanmei.com", "zelong.huangquechuanmei.com"
    }:
        raise BrowserQAError("客户旅程只允许黄雀正式站或泽龙测试站")
    return {
        "name": cookie_name, "value": token, "url": origin,
        "httpOnly": True, "secure": True, "sameSite": "Lax",
    }


def run_nb2_reference_journey(*, origin, account_token, cookie_name, fixture_path,
                              prompt, expected_cost, run_id, refresh_token, on_job,
                              timeout_seconds=300):
    """Run one real customer submission and return private, token-free evidence."""
    try:
        from PIL import Image
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserQAError("服务器缺少浏览器质检运行环境") from exc

    fixture = pathlib.Path(fixture_path).resolve()
    if not fixture.is_file():
        raise BrowserQAError("私有参考图测试包不存在")
    origin = str(origin or "").rstrip("/")
    target = origin + "/workbench/banana.html"
    steps = []
    request_state = {"seen": False, "idempotency_key": "", "digest": "", "problem": ""}
    response_state = {"response": None}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        try:
            context = browser.new_context(accept_downloads=True, viewport={"width": 1440, "height": 980})
            context.add_cookies([_cookie(account_token, origin, cookie_name)])
            context.add_init_script("""
                localStorage.removeItem('hq_last_result');
                localStorage.removeItem('hq_last_result_ratio');
                localStorage.removeItem('hq_active_jobs');
                localStorage.removeItem('hq_active_job');
            """)
            page = context.new_page()

            def intercept(route, request):
                if request.method != "POST":
                    return route.continue_()
                request_state["seen"] = True
                try:
                    payload = request.post_data_json
                    validate_nb2_reference_payload(payload, prompt)
                    payload["qa_operation_id"] = "image.banana.nb2.reference"
                    payload["qa_run_id"] = run_id
                    headers = dict(request.headers)
                    headers.pop("content-length", None)
                    request_state["idempotency_key"] = headers.get("idempotency-key", "")
                    if not request_state["idempotency_key"]:
                        raise BrowserQAError("客户提交缺少幂等键")
                    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                    request_state["digest"] = hashlib.sha256(encoded.encode()).hexdigest()
                    route.continue_(headers=headers, post_data=encoded)
                except Exception as exc:
                    request_state["problem"] = str(exc)[:220]
                    route.abort()

            def remember_response(response):
                if (urllib.parse.urlparse(response.url).path == "/api/gen/banana"
                        and response.request.method == "POST"):
                    response_state["response"] = response

            page.route("**/api/gen/banana", intercept)
            page.on("response", remember_response)
            page.goto(target, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector("#bGen", state="visible", timeout=20000)
            steps.append(_capture_step(page, "entry", "真实客户图片生成页可交互"))

            page.locator('#engineRow [data-engine="banana"]').click()
            page.locator('#bananaVariantRow [data-variant="nb2"]').click()
            page.locator('#qualityRow [data-q="std"]').click()
            page.locator("#countInput").fill("1")
            page.locator("#countInput").press("Enter")
            page.locator("#upFile").set_input_files(str(fixture))
            page.wait_for_selector(".image-ref-thumb", state="visible", timeout=20000)
            page.locator(PROMPT_EDITOR_SELECTOR).fill(prompt)
            steps.append(_capture_step(page, "fixture", "1 张私有参考图和预设提示词已加载"))

            page.locator("#bGen").click()
            deadline, response, body = time.time() + 45, None, {}
            while time.time() < deadline:
                while time.time() < deadline and response_state["response"] is None:
                    if request_state["problem"]:
                        raise BrowserQAError(request_state["problem"])
                    page.wait_for_timeout(250)
                response = response_state["response"]
                if response is None:
                    break
                try:
                    body = response.json()
                except Exception as exc:
                    raise BrowserSubmitUncertain("业务接口响应无法解析") from exc
                if response.status == 503 and body.get("code") == "shutting_down":
                    response_state["response"] = None  # 客户页会用同一幂等键安全重试
                    continue
                break
            if response is None:
                if request_state["seen"]:
                    raise BrowserSubmitUncertain("客户提交已发出，但业务接口没有返回可核对响应")
                raise BrowserQAError("点击生成后没有发出图片生成请求")
            if response.status == 409 and body.get("code") == "idempotency_conflict":
                raise BrowserQAError("客户页幂等键与其他请求冲突")
            if response.status >= 500 or response.status in {408, 409}:
                raise BrowserSubmitUncertain(str(body.get("detail") or "业务接口响应不确定"))
            if response.status >= 400:
                raise BrowserQAError(str(body.get("detail") or "业务接口拒绝客户测试"))
            try:
                job_id = int(body["job_id"])
                points_after = int(body["points_left"])
                actual_cost = int(body["cost"])
            except (KeyError, TypeError, ValueError) as exc:
                raise BrowserSubmitUncertain("业务接口没有返回 job_id、实际扣点或点数余额") from exc
            try:
                on_job({
                    "job_id": job_id,
                    "actual_cost": actual_cost,
                    "points_after": points_after,
                    "idempotency_key": request_state["idempotency_key"],
                    "request_sha256": request_state["digest"],
                })
            except Exception as exc:
                raise BrowserSubmitUncertain("任务已受理，但本地 job_id 证据写入失败") from exc
            if actual_cost != int(expected_cost):
                raise BrowserQAError("实时价格已从 %s 点变化为 %s 点；任务已保留待核对" % (
                    expected_cost, actual_cost,
                ))
            steps.append(_capture_step(page, "submit", "业务接口返回 job_id=%s" % job_id))

            result_url = ""
            deadline = time.time() + max(30, int(timeout_seconds))
            refresh_at = time.time() + 75
            while time.time() < deadline:
                if time.time() >= refresh_at:
                    context.add_cookies([_cookie(refresh_token(), origin, cookie_name)])
                    refresh_at = time.time() + 75
                result_url = page.evaluate("localStorage.getItem('hq_last_result') || ''")
                if result_url:
                    break
                note = page.locator("#bNote").inner_text()
                if "失败" in note or "已退点" in note:
                    raise BrowserQAError(note)
                page.wait_for_timeout(1000)
            if not result_url:
                raise BrowserQAError("客户页在限定时间内没有展示生成作品")
            page.wait_for_function(
                "document.querySelector('#bResult').complete && document.querySelector('#bResult').naturalWidth > 0",
                timeout=20000,
            )
            steps.append(_capture_step(page, "result", "作品已回写客户结果区"))

            with tempfile.TemporaryDirectory(prefix="hq-browser-qa-") as tmp:
                with page.expect_download(timeout=30000) as pending:
                    page.locator("#actRow > *").filter(has_text="下载").first.click()
                download = pending.value
                saved = pathlib.Path(tmp) / "result"
                download.save_as(str(saved))
                raw = saved.read_bytes()
                with Image.open(saved) as image:
                    image.verify()
                with Image.open(saved) as image:
                    image_meta = {
                        "format": str(image.format or "").upper(),
                        "width": int(image.width), "height": int(image.height),
                    }
            steps.append(_capture_step(page, "download", "下载文件存在、非空且可解码"))
            steps.append(_capture_step(page, "correlation", "客户页任务已绑定 job_id=%s" % job_id))
            return {
                "status": "passed", "executor": "playwright",
                "checks": steps, "passed": len(steps), "total": len(CHECK_NAMES),
                "job_id": job_id, "result_url_sha256": hashlib.sha256(result_url.encode()).hexdigest(),
                "download": dict(image_meta, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()),
                "completed_at": int(time.time()),
            }
        finally:
            browser.close()
