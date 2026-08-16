# Pixelle Material Style Presets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a server-controlled material-style selector to the text-to-video workbench so one approved style consistently drives every generated image or video asset in a job.

**Architecture:** Keep the style catalog and full English `prompt_prefix` values in the main-site backend. The authenticated page fetches only public style keys and Chinese names, submits the selected key with the existing paid/idempotent job request, and the Pixelle adapter resolves the key to a trusted prefix immediately before calling Pixelle. Template type continues to select the existing image or video workflow independently of style.

**Tech Stack:** Python 3 standard library backend, `unittest`, single-page HTML/CSS/JavaScript workbench, Pixelle asynchronous `/api/video/generate/async` contract.

## Global Constraints

- Work only in `D:\codex\huangque-material-styles` on branch `codex/pixelle-material-styles-20260808`.
- Add exactly 10 styles with keys `realistic_commercial`, `cinematic`, `future_tech`, `healing_fresh`, `chinese_illustration`, `cartoon_3d`, `retro_film`, `minimal_line`, `medical_beauty`, and `ecommerce_product`.
- Default style is exactly `realistic_commercial`.
- A job has one style for all scenes; do not add per-scene style selection.
- Use a standard text dropdown; do not add style thumbnails.
- The browser receives only `key` and Chinese `name`; it must never receive or submit `prompt_prefix`.
- Reject an unknown style in `prepare_payload()` before job creation and point charging with `请选择有效的素材风格`.
- Both illustration and video templates use the selected style, while preserving `PIXELLE_MEDIA_WORKFLOW` and `PIXELLE_VIDEO_WORKFLOW` selection by template kind.
- Do not change Pixelle/RunningHub workflow files, pricing, TTS, captions, uploads, or existing template previews.
- Do not log, persist, or return the full style prefix in public APIs or job results.
- If the style catalog cannot load, keep generation disabled and show `素材风格暂不可用`.
- Create a new Draft PR only. Do not merge or deploy.

## File Map

- Modify `server/content_domains/pixelle_video.py`: own the private style catalog, expose a sanitized public catalog, validate `style`, map it to `prompt_prefix`, and return the style key in generated metadata.
- Modify `server/content_domains/core.py`: expose the authenticated, readiness-gated `GET /api/gen/text-video/styles` endpoint.
- Modify `site/workbench/text-video.html`: render the no-thumbnail style dropdown, load its options, fail closed, and include `style` in the existing idempotent request body.
- Modify `tests/test_pixelle_video.py`: cover catalog integrity, pre-charge validation, upstream contract, workflow independence, and safe result metadata.
- Modify `tests/test_text_video_page.py`: cover authenticated routing, dropdown behavior, payload/idempotency inclusion, and failure gating.

---

### Task 1: Private Style Catalog And Pre-Charge Validation

**Files:**
- Modify: `server/content_domains/pixelle_video.py:37-165`
- Test: `tests/test_pixelle_video.py:17-138`

**Interfaces:**
- Produces: `DEFAULT_STYLE: str = "realistic_commercial"`.
- Produces: `STYLE_PRESETS: tuple[dict[str, str], ...]`, where each item has `key`, `name`, and `prompt_prefix`.
- Produces: `STYLE_PRESETS_BY_KEY: dict[str, dict[str, str]]`.
- Produces: `public_styles() -> list[dict[str, str]]`, returning only `key` and `name`.
- Changes: `prepare_payload(payload: Mapping) -> dict` adds a validated `style` key.

- [ ] **Step 1: Add failing catalog and validation tests**

Add these tests to `PixelleVideoTests`:

```python
def test_public_style_catalog_matches_private_allowlist(self):
    styles = self.pixelle.public_styles()
    self.assertEqual(len(styles), 10)
    self.assertEqual(len({item["key"] for item in styles}), 10)
    self.assertEqual(
        [item["key"] for item in styles],
        [
            "realistic_commercial",
            "cinematic",
            "future_tech",
            "healing_fresh",
            "chinese_illustration",
            "cartoon_3d",
            "retro_film",
            "minimal_line",
            "medical_beauty",
            "ecommerce_product",
        ],
    )
    self.assertEqual(self.pixelle.DEFAULT_STYLE, "realistic_commercial")
    self.assertTrue(all(set(item) == {"key", "name"} for item in styles))
    self.assertTrue(all("prompt_prefix" not in item for item in styles))
    self.assertTrue(all(self.pixelle.STYLE_PRESETS_BY_KEY[item["key"]]["prompt_prefix"] for item in styles))

def test_prepare_uses_default_and_preserves_selected_style(self):
    default = self.pixelle.prepare_payload({"text": "AI 培训"})
    selected = self.pixelle.prepare_payload({
        "text": "AI 培训",
        "style": "future_tech",
    })
    self.assertEqual(default["style"], "realistic_commercial")
    self.assertEqual(selected["style"], "future_tech")

def test_prepare_rejects_invalid_style_before_charge(self):
    with self.assertRaisesRegex(ValueError, "请选择有效的素材风格"):
        self.pixelle.prepare_payload({
            "text": "AI 培训",
            "style": "custom prompt injection",
        })
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
python -m unittest `
  tests.test_pixelle_video.PixelleVideoTests.test_public_style_catalog_matches_private_allowlist `
  tests.test_pixelle_video.PixelleVideoTests.test_prepare_uses_default_and_preserves_selected_style `
  tests.test_pixelle_video.PixelleVideoTests.test_prepare_rejects_invalid_style_before_charge -v
```

Expected: FAIL because `public_styles`, `DEFAULT_STYLE`, and `STYLE_PRESETS_BY_KEY` do not exist and `prepare_payload()` does not validate `style`.

- [ ] **Step 3: Add the private catalog and sanitized public projection**

Place this catalog after the workflow/environment constants and before template-name constants:

```python
DEFAULT_STYLE = "realistic_commercial"
_STYLE_COMMON_RESTRICTIONS = (
    "No watermark, no logo, no garbled or unreadable text, "
    "no malformed people, objects, hands, or anatomy."
)
STYLE_PRESETS = (
    {
        "key": "realistic_commercial",
        "name": "写实商业",
        "prompt_prefix": (
            "Photorealistic commercial advertising, authentic people or products, "
            "modern business environment, natural professional lighting, credible "
            "editorial composition, realistic materials and balanced colors. "
            + _STYLE_COMMON_RESTRICTIONS
        ),
    },
    {
        "key": "cinematic",
        "name": "电影质感",
        "prompt_prefix": (
            "Cinematic visual storytelling, motivated film lighting, narrative composition, "
            "shallow depth of field, restrained color grading, realistic texture, subtle film grain. "
            + _STYLE_COMMON_RESTRICTIONS
        ),
    },
    {
        "key": "future_tech",
        "name": "科技未来",
        "prompt_prefix": (
            "Premium near-future AI visual design, clean advanced spaces, precise blue and cyan "
            "accent lighting, refined interface motifs, crisp geometry, high-end technology campaign. "
            + _STYLE_COMMON_RESTRICTIONS
        ),
    },
    {
        "key": "healing_fresh",
        "name": "治愈清新",
        "prompt_prefix": (
            "Bright healing lifestyle visual, soft natural daylight, gentle low-saturation palette, "
            "airy everyday setting, calm composition, light and optimistic atmosphere. "
            + _STYLE_COMMON_RESTRICTIONS
        ),
    },
    {
        "key": "chinese_illustration",
        "name": "国风插画",
        "prompt_prefix": (
            "Contemporary Chinese illustration, elegant ink wash and fine brush textures, intentional "
            "negative space, refined oriental palette, poetic layered composition, delicate paper texture. "
            + _STYLE_COMMON_RESTRICTIONS
        ),
    },
    {
        "key": "cartoon_3d",
        "name": "3D 卡通",
        "prompt_prefix": (
            "Polished 3D cartoon scene, appealing rounded characters or objects, soft tactile materials, "
            "bright studio lighting, expressive but coherent forms, premium animated-film rendering. "
            + _STYLE_COMMON_RESTRICTIONS
        ),
    },
    {
        "key": "retro_film",
        "name": "复古胶片",
        "prompt_prefix": (
            "Documentary retro film aesthetic, organic analog grain, soft highlight roll-off, nostalgic "
            "muted colors, natural candid composition, authentic period camera texture. "
            + _STYLE_COMMON_RESTRICTIONS
        ),
    },
    {
        "key": "minimal_line",
        "name": "极简线稿",
        "prompt_prefix": (
            "Minimal line-art illustration, precise clean strokes, limited color palette, generous negative "
            "space, editorial infographic composition, clear visual hierarchy, refined paper-white ground. "
            + _STYLE_COMMON_RESTRICTIONS
        ),
    },
    {
        "key": "medical_beauty",
        "name": "医美高级感",
        "prompt_prefix": (
            "Premium medical-aesthetics campaign, immaculate contemporary clinic, soft clean lighting, "
            "natural skin texture, restrained luxury, trustworthy professional mood, elegant neutral palette. "
            + _STYLE_COMMON_RESTRICTIONS
        ),
    },
    {
        "key": "ecommerce_product",
        "name": "电商产品广告",
        "prompt_prefix": (
            "High-conversion ecommerce product advertising, unmistakable hero product, controlled studio "
            "lighting, crisp material details, clean background, premium commercial composition and depth. "
            + _STYLE_COMMON_RESTRICTIONS
        ),
    },
)
STYLE_PRESETS_BY_KEY = {item["key"]: item for item in STYLE_PRESETS}
```

Add the public projection beside `public_templates()`:

```python
def public_styles():
    return [
        {"key": item["key"], "name": item["name"]}
        for item in STYLE_PRESETS
    ]
```

- [ ] **Step 4: Validate style in `prepare_payload()`**

Immediately after template validation, resolve and validate the submitted key:

```python
style = str(body.get("style") or DEFAULT_STYLE).strip()
if style not in STYLE_PRESETS_BY_KEY:
    raise ValueError("请选择有效的素材风格")
```

Add `"style": style` to the returned prepared payload. Do not copy `prompt_prefix` into the prepared payload.

- [ ] **Step 5: Run focused tests and the adapter suite**

Run:

```powershell
python -m unittest tests.test_pixelle_video -v
```

Expected: all Pixelle adapter tests PASS; existing template, workflow, availability, and download tests remain green.

- [ ] **Step 6: Commit the catalog and validation**

```powershell
git add server/content_domains/pixelle_video.py tests/test_pixelle_video.py
git commit -m "feat: add Pixelle material style catalog"
```

---

### Task 2: Authenticated Public Style Endpoint

**Files:**
- Modify: `server/content_domains/core.py:3012-3025`
- Test: `tests/test_text_video_page.py:59-64`

**Interfaces:**
- Consumes: `pixelle_video.public_styles() -> list[dict[str, str]]` and `pixelle_video.DEFAULT_STYLE` from Task 1.
- Produces: authenticated `GET /api/gen/text-video/styles`.
- Produces response: `{"styles": [{"key": str, "name": str}], "default_style": str}`.
- Preserves: `pixelle_video.require_available()` readiness gate used by the template catalog.

- [ ] **Step 1: Add a failing route-contract test**

First replace the brittle route-set assertion in
`test_template_catalog_is_authenticated_and_not_hardcoded_to_service`:

```python
for path in (
    "/api/gen/text-video/capability",
    "/api/gen/text-video/templates",
):
    self.assertIn(path, CORE)
```

Then add this separate backend-route test. It deliberately does not assert page behavior; the page starts consuming the endpoint in Task 4:

```python
def test_style_catalog_is_authenticated_sanitized_and_readiness_gated(self):
    for path in (
        "/api/gen/text-video/capability",
        "/api/gen/text-video/templates",
        "/api/gen/text-video/styles",
    ):
        self.assertIn(path, CORE)
    self.assertIn('"styles": pixelle_video.public_styles()', CORE)
    self.assertIn('"default_style": pixelle_video.DEFAULT_STYLE', CORE)
```

- [ ] **Step 2: Run the route test and verify RED**

Run:

```powershell
python -m unittest tests.test_text_video_page.TextVideoPageTests.test_style_catalog_is_authenticated_sanitized_and_readiness_gated -v
```

Expected: FAIL because the styles endpoint and response do not exist.

- [ ] **Step 3: Extend the authenticated GET route**

Change the route set to include the new endpoint:

```python
if p in {
    "/api/gen/text-video/capability",
    "/api/gen/text-video/templates",
    "/api/gen/text-video/styles",
}:
```

Keep the existing authentication and `require_available()` code. Replace the final unconditional template response with explicit responses:

```python
if p == "/api/gen/text-video/templates":
    return self._send(200, {"templates": pixelle_video.public_templates()})
return self._send(200, {
    "styles": pixelle_video.public_styles(),
    "default_style": pixelle_video.DEFAULT_STYLE,
})
```

This response must not reference `STYLE_PRESETS`, `STYLE_PRESETS_BY_KEY`, or `prompt_prefix`.

- [ ] **Step 4: Run focused route and page tests**

Run:

```powershell
python -m unittest tests.test_text_video_page -v
```

Expected: all page contract tests PASS.

- [ ] **Step 5: Commit the endpoint**

```powershell
git add server/content_domains/core.py tests/test_text_video_page.py
git commit -m "feat: expose Pixelle material styles"
```

---

### Task 3: Trusted Prefix Submission And Safe Result Metadata

**Files:**
- Modify: `server/content_domains/pixelle_video.py:225-341`
- Test: `tests/test_pixelle_video.py:109-138,207-223`

**Interfaces:**
- Consumes: prepared `payload["style"]: str` and `STYLE_PRESETS_BY_KEY` from Task 1.
- Changes: `_submit(payload)` adds trusted `prompt_prefix` to Pixelle's asynchronous request body.
- Changes: `generate(payload)` returns `style` as a key only.
- Preserves: `media_workflow` selection remains based only on `TEMPLATES_BY_KEY[payload["template"]]["kind"]`.

- [ ] **Step 1: Add failing submission-contract tests**

Update the existing image-template submission test to prepare `medical_beauty` and assert the trusted prefix:

```python
payload = self.pixelle.prepare_payload({
    "text": "AI 培训",
    "mode": "generate",
    "style": "medical_beauty",
})
# existing _submit mock and request assertions
self.assertEqual(
    body["prompt_prefix"],
    self.pixelle.STYLE_PRESETS_BY_KEY["medical_beauty"]["prompt_prefix"],
)
self.assertEqual(body["media_workflow"], self.pixelle.PIXELLE_MEDIA_WORKFLOW)
```

Update the video-template test to use the same selected style and assert both prefix and video workflow:

```python
payload = self.pixelle.prepare_payload({
    "text": "AI 培训",
    "mode": "generate",
    "template": "1080x1920/video_default.html",
    "style": "medical_beauty",
})
# existing _submit mock
self.assertEqual(
    body["prompt_prefix"],
    self.pixelle.STYLE_PRESETS_BY_KEY["medical_beauty"]["prompt_prefix"],
)
self.assertEqual(body["media_workflow"], self.pixelle.PIXELLE_VIDEO_WORKFLOW)
```

Extend `test_generate_persists_service_result_in_authenticated_asset_path`:

```python
self.assertEqual(result["style"], payload["style"])
self.assertNotIn("prompt_prefix", result)
```

- [ ] **Step 2: Run submission and result tests and verify RED**

Run:

```powershell
python -m unittest `
  tests.test_pixelle_video.PixelleVideoTests.test_submit_uses_async_service_contract `
  tests.test_pixelle_video.PixelleVideoTests.test_submit_video_template_uses_video_workflow `
  tests.test_pixelle_video.PixelleVideoTests.test_generate_persists_service_result_in_authenticated_asset_path -v
```

Expected: FAIL because the upstream request has no `prompt_prefix` and the result has no `style`.

- [ ] **Step 3: Map the key to a trusted prefix at submission time**

Inside `_submit()`, after resolving the template, resolve the server-owned style:

```python
style = STYLE_PRESETS_BY_KEY[payload["style"]]
```

Add this field to the body passed to `_json_request()`:

```python
"prompt_prefix": style["prompt_prefix"],
```

Do not accept a prefix from `payload`; `_submit()` must exclusively use `STYLE_PRESETS_BY_KEY`.

- [ ] **Step 4: Record only the style key in generated metadata**

Add this field to `generate()`'s result dictionary:

```python
"style": payload["style"],
```

Do not add the prefix to the result dictionary or download metadata.

- [ ] **Step 5: Run the complete adapter suite**

Run:

```powershell
python -m unittest tests.test_pixelle_video -v
```

Expected: all Pixelle tests PASS, including distinct image/video workflow assertions with the same selected style.

- [ ] **Step 6: Commit trusted style application**

```powershell
git add server/content_domains/pixelle_video.py tests/test_pixelle_video.py
git commit -m "feat: apply selected Pixelle material style"
```

---

### Task 4: No-Thumbnail Style Selector And Fail-Closed Page State

**Files:**
- Modify: `site/workbench/text-video.html:14-240`
- Test: `tests/test_text_video_page.py:15-105`

**Interfaces:**
- Consumes: `GET /api/gen/text-video/styles` response from Task 2.
- Produces DOM: `<select id="materialStyle" aria-label="素材风格" disabled>`.
- Produces state: `stylesReady: boolean`, `isBusy: boolean`.
- Changes request body: adds `style: el('materialStyle').value`.
- Preserves: selected style across template kind, orientation, template, and input-mode changes during the page lifetime.

- [ ] **Step 1: Add failing page behavior tests**

Add these tests to `TextVideoPageTests`:

```python
def test_material_style_dropdown_loads_public_catalog_without_thumbnails(self):
    self.assertIn('id="materialStyle"', PAGE)
    self.assertIn('aria-label="素材风格"', PAGE)
    self.assertIn("/api/gen/text-video/styles", PAGE)
    self.assertIn("response.data.default_style", PAGE)
    self.assertIn("option.value=style.key", PAGE)
    self.assertIn("option.textContent=style.name", PAGE)
    self.assertNotIn("style.preview_url", PAGE)

def test_generation_requires_loaded_style_and_submits_it_idempotently(self):
    self.assertIn("if(!stylesReady)", PAGE)
    self.assertIn("素材风格暂不可用", PAGE)
    self.assertIn("style:el('materialStyle').value", PAGE)
    self.assertIn("var pending=pendingSubmission(payload)", PAGE)

def test_style_load_failure_keeps_generation_disabled(self):
    self.assertIn("stylesReady=false", PAGE)
    self.assertIn("syncGenerateButton()", PAGE)
    self.assertIn("el('materialStyle').disabled=true", PAGE)
```

Update the Node idempotency fixture payload so the serialization check covers style:

```javascript
const payload = {
  pipeline: 'pixelle',
  text: 'test',
  style: 'future_tech',
};
```

- [ ] **Step 2: Run page tests and verify RED**

Run:

```powershell
python -m unittest tests.test_text_video_page -v
```

Expected: FAIL because the selector, loader, ready state, and style request field do not exist.

- [ ] **Step 3: Add the accessible dropdown without thumbnails**

Add a compact field below the template grid and above input mode:

```html
<div class="tv-style-field">
  <label for="materialStyle">素材风格</label>
  <select id="materialStyle" aria-label="素材风格" disabled>
    <option value="">正在加载素材风格</option>
  </select>
</div>
```

Start the generation button disabled:

```html
<button id="generateBtn" class="tv-action" type="button" disabled>生成视频</button>
```

Add CSS that matches the existing dark workbench and fits mobile widths:

```css
.tv-style-field{display:grid;gap:8px;margin:18px 0}
.tv-style-field label{color:#cbd6e5;font-size:12px;font-weight:700}
.tv-style-field select{width:100%;height:42px;padding:0 36px 0 12px;border:1px solid rgba(148,164,187,.22);border-radius:6px;background:#0b111d;color:#f1f5fa;font:13px inherit;letter-spacing:0;outline:none}
.tv-style-field select:focus{border-color:#e7b24c;box-shadow:0 0 0 2px rgba(231,178,76,.14)}
.tv-style-field select:disabled{color:#65748a;cursor:not-allowed}
```

- [ ] **Step 4: Introduce explicit busy and style-readiness button state**

Extend the page state declaration:

```javascript
var token='__cookie__',activeBlob='',startedAt=0,pollTimer=0,
    activeTemplate='',activeMode='generate',templates=[],activeKind='illustration',
    activeOrientation='portrait',stylesReady=false,isBusy=false;
```

Replace the current `setBusy()` with:

```javascript
function syncGenerateButton(){
  el('generateBtn').disabled=isBusy||!stylesReady;
  el('generateBtn').textContent=isBusy?'正在生成':'生成视频';
}
function setBusy(value){isBusy=value;syncGenerateButton();}
```

- [ ] **Step 5: Load and sanitize the style options**

Add this loader beside `loadTemplates()`:

```javascript
function loadStyles(){
  var select=el('materialStyle');
  stylesReady=false;select.disabled=true;syncGenerateButton();
  fetch('/api/gen/text-video/styles',{cache:'no-store',headers:authHeaders(false)})
    .then(readJson)
    .then(function(response){
      var styles=response.data.styles||[],defaultStyle=response.data.default_style||'';
      if(!styles.length||!defaultStyle)throw new Error(response.data.detail||'没有可用素材风格');
      select.textContent='';
      styles.forEach(function(style){
        var option=document.createElement('option');
        option.value=style.key;option.textContent=style.name;select.appendChild(option);
      });
      if(!styles.some(function(style){return style.key===defaultStyle;}))throw new Error('默认素材风格无效');
      select.value=defaultStyle;select.disabled=false;stylesReady=true;syncGenerateButton();
    })
    .catch(function(){
      stylesReady=false;select.disabled=true;select.innerHTML='<option value="">素材风格暂不可用</option>';
      syncGenerateButton();setStatus('素材风格暂不可用','error');
    });
}
```

Call both loaders at startup so the fetches begin independently:

```javascript
updateCount();loadTemplates();loadStyles();
```

Do not call `loadStyles()` from `selectMode`, `selectKind`, `selectOrientation`, or `selectTemplate`; that preserves the selected style while those controls change.

- [ ] **Step 6: Require and submit the selected style**

In `generate()`, after template readiness validation, add:

```javascript
if(!stylesReady){toast('素材风格暂不可用');return;}
var style=el('materialStyle').value;
if(!style){toast('请选择素材风格');return;}
```

Build the request body with the selected key:

```javascript
var payload={
  pipeline:'pixelle',
  text:text,
  template:activeTemplate,
  mode:activeMode,
  style:el('materialStyle').value
};
```

Because `pendingSubmission()` serializes the full payload, a style change naturally creates a new idempotency identity while a retry of the same style reuses the existing key.

- [ ] **Step 7: Run focused frontend tests**

Run:

```powershell
python -m unittest tests.test_text_video_page -v
```

Expected: all page tests PASS, including the Node-based idempotency check.

- [ ] **Step 8: Run both focused suites together**

Run:

```powershell
python -m unittest tests.test_pixelle_video tests.test_text_video_page -v
```

Expected: all focused backend and frontend tests PASS.

- [ ] **Step 9: Commit the selector**

```powershell
git add site/workbench/text-video.html tests/test_text_video_page.py
git commit -m "feat: add material style selector"
```

---

### Task 5: Browser Acceptance, Regression Checks, And Draft PR

**Files:**
- Verify: `site/workbench/text-video.html`
- Verify: `server/content_domains/pixelle_video.py`
- Verify: `server/content_domains/core.py`
- Verify: `tests/test_pixelle_video.py`
- Verify: `tests/test_text_video_page.py`

**Interfaces:**
- Verifies the public browser contract and request body without starting a paid generation.
- Produces a pushed feature branch and one Draft PR; does not merge or deploy.

- [ ] **Step 1: Run syntax and focused regression checks**

Run:

```powershell
python -m compileall -q server
python -m unittest tests.test_pixelle_video tests.test_text_video_page -v
```

Expected: compile command exits 0 and all focused tests PASS.

- [ ] **Step 2: Run the repository test baseline**

Run:

```powershell
python -m unittest discover -s tests -v
```

Expected: full baseline PASS. If an unrelated pre-existing test fails, capture its exact test name and output, verify it also fails on the current `origin/main`, and report it separately instead of changing unrelated code.

- [ ] **Step 3: Confirm the existing local test workbench is reachable**

The user's local test workbench is expected at port 8765. Verify it before browser work:

```powershell
(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8765/workbench/text-video.html).StatusCode
```

Expected: `200`. If the server is not running, start the same local Huangque test stack that owns port 8765, then repeat this exact request. Do not point browser acceptance at production and do not click through a paid generation; intercept `/api/gen/script_to_video` before submission.

Expected: `text-video.html` loads through the local authenticated workbench. With an authenticated session and the Pixelle readiness gate available, both catalog endpoints return 200.

- [ ] **Step 4: Verify desktop behavior at 1440 x 900**

Using the in-app browser:

1. Open the local `/workbench/text-video.html` page.
2. Confirm the material-style control is a dropdown, not a thumbnail gallery.
3. Confirm exactly 10 Chinese options and default `写实商业`.
4. Select `医美高级感`, switch illustration/video template tabs, switch portrait/landscape where available, and switch topic/full-copy mode.
5. Confirm `医美高级感` remains selected and no text overlaps or horizontal overflow appears.
6. Intercept the submit request or replace `window.fetch` for `/api/gen/script_to_video`; confirm JSON contains `"style":"medical_beauty"` and contains no `prompt_prefix`.

Expected: all six checks pass without creating a paid job.

- [ ] **Step 5: Verify mobile behavior at 390 x 844**

Using the in-app browser responsive viewport:

1. Confirm the dropdown fits the editor width.
2. Open all 10 options and confirm the longest label is readable.
3. Confirm the dropdown does not overlap the template gallery, input-mode control, textarea, or generation button.
4. Confirm selecting `电商产品广告` yields `ecommerce_product` in an intercepted request body.

Expected: no overlap, clipping, or horizontal page scroll; request key is correct.

- [ ] **Step 6: Verify fail-closed behavior**

Block or mock `GET /api/gen/text-video/styles` to return 503, reload the page, and confirm:

- dropdown remains disabled;
- generation button remains disabled;
- page status displays `素材风格暂不可用`;
- no `/api/gen/script_to_video` request can be created.

Expected: all four fail-closed checks pass.

- [ ] **Step 7: Inspect changes and secrets before publication**

Run:

```powershell
git status --short
git diff --check
git diff origin/main...HEAD --stat
git diff origin/main...HEAD -- `
  server/content_domains/pixelle_video.py `
  server/content_domains/core.py `
  site/workbench/text-video.html `
  tests/test_pixelle_video.py `
  tests/test_text_video_page.py `
  docs/superpowers/specs/2026-08-08-pixelle-material-styles-design.md `
  docs/superpowers/plans/2026-08-08-pixelle-material-styles.md
rg -n "sk-[A-Za-z0-9_-]{16,}|Authorization: Bearer [^$]" `
  server/content_domains/pixelle_video.py `
  server/content_domains/core.py `
  site/workbench/text-video.html `
  tests/test_pixelle_video.py `
  tests/test_text_video_page.py
```

Expected: clean whitespace check, only intended files changed, and secret scan returns no matches.

- [ ] **Step 8: Refresh the remote base and re-run checks if rebased**

Run:

```powershell
git fetch origin --prune
git rebase origin/main
python -m unittest tests.test_pixelle_video tests.test_text_video_page -v
git diff --check
```

Expected: rebase succeeds and focused tests remain green. Resolve only conflicts in the intended files; do not overwrite unrelated upstream changes.

- [ ] **Step 9: Push the branch and open a Draft PR**

Run:

```powershell
git push -u origin codex/pixelle-material-styles-20260808
gh pr create --draft `
  --repo tang730125633/huangque-main-site `
  --base main `
  --head codex/pixelle-material-styles-20260808 `
  --title "feat: add Pixelle material style presets" `
  --body "Adds 10 server-controlled material styles to the text-video workbench. The authenticated frontend submits only a style key; the backend validates it before charging and maps it to a trusted Pixelle prompt prefix while preserving existing image/video workflow selection. Includes fail-closed UI loading and focused backend/frontend tests. No deployment is included."
```

Expected: GitHub returns a new Draft PR URL. Stop after reporting the PR number, exact head commit, test results, and any remaining browser or baseline risk.
