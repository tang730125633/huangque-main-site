# Text Video Speech Rate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users control narration speed for both public and personal voices in text-to-video generation.

**Architecture:** The browser submits a normalized `speech_rate`; the main-site adapter validates it once. Public voices map it to Pixelle `tts_speed`, while personal voices pass it to the existing CosyVoice synthesis call and continue supplying external narration audio.

**Tech Stack:** HTML/CSS/vanilla JavaScript, Python content-domain adapters, unittest.

## Global Constraints

- UI range is `0.5x` through `2.0x`, step `0.1x`, default `1.0x`.
- Server rejects booleans, non-numeric values, non-finite values, and out-of-range values.
- Personal narration is changed only during synthesis and must not send `tts_speed` to Pixelle.
- Public narration sends `tts_speed` to Pixelle.
- Submit a PR only; do not merge or deploy.

---

### Task 1: Validate and route speech rate in the content domain

**Files:**
- Modify: `server/content_domains/pixelle_video.py`
- Modify: `tests/test_pixelle_video.py`

**Interfaces:**
- Consumes: payload field `speech_rate`.
- Produces: normalized `payload["speech_rate"]: float`; public request `body["tts_speed"]`; personal synthesis keyword `speed`.

- [ ] **Step 1: Write failing validation and routing tests**

Cover default `1.0`, normalization to one decimal, rejection of `True`, strings, NaN, infinities, `0.4`, and `2.1`; assert public request body contains `tts_speed`; assert every personal `synthesize_owned_voice_segment` call receives `speed=payload["speech_rate"]` and the Pixelle body omits `tts_speed`.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python -m unittest tests.test_pixelle_video`

Expected: new speech-rate assertions fail.

- [ ] **Step 3: Implement one validation helper and route-specific behavior**

Add a private helper that converts numeric input to a finite float, enforces `0.5 <= value <= 2.0`, and rounds to one decimal. Call it from `prepare_payload`. Add `tts_speed` for public/default Pixelle TTS requests. Pass `speed=` during personal segment synthesis, then remove/omit `tts_speed` when `narration_segments` is used.

- [ ] **Step 4: Run focused tests**

Run: `python -m unittest tests.test_pixelle_video tests.test_text_video_personal_audio`

Expected: PASS.

### Task 2: Add the speech-rate control to the page

**Files:**
- Modify: `site/workbench/text-video.html`
- Modify: `tests/test_text_video_page.py`

**Interfaces:**
- Consumes: range input `#speechRate`.
- Produces: numeric payload field `speech_rate` and visible `#speechRateValue` text.

- [ ] **Step 1: Write failing static UI contract tests**

Require an accessible range input with `min="0.5"`, `max="2"`, `step="0.1"`, `value="1"`; require a visible `1.0x` value; require `speech_rate:Number(el('speechRate').value)` in the generation payload and an input listener that updates the label.

- [ ] **Step 2: Run the page test and verify failure**

Run: `python -m unittest tests.test_text_video_page`

Expected: new assertions fail.

- [ ] **Step 3: Implement the compact slider**

Place the control directly below voice selection. Keep dimensions stable, use the existing form styling, and update the text label with one decimal place on every input event. Do not reset it when voice selection changes.

- [ ] **Step 4: Run page and content-domain regressions**

Run: `python -m unittest tests.test_text_video_page tests.test_pixelle_video tests.test_text_video_personal_audio`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add site/workbench/text-video.html server/content_domains/pixelle_video.py tests/test_text_video_page.py tests/test_pixelle_video.py docs/superpowers/specs/2026-08-11-text-video-speech-rate-design.md docs/superpowers/plans/2026-08-11-text-video-speech-rate.md
git commit -m "feat(text-video): add narration speed control"
```

### Task 3: Publish the dependent main-site PR

**Files:** No source changes.

**Interfaces:**
- Consumes: merged or review-ready generation-server PR supporting `tts_speed`.
- Produces: main-site PR URL with the generation-server dependency linked.

- [ ] **Step 1: Run final verification**

Run focused tests, `git diff --check origin/main...HEAD`, inspect changed files, and verify that no secrets or unrelated files are included.

- [ ] **Step 2: Push the branch and open a PR**

Push `codex/text-video-speech-rate` and open a PR targeting `main`. Link the generation-server PR and state that merge/deployment order is generation server first, main site second.
