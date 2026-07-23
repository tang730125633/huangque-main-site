# Image Upload Feedback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reject oversized reference images before upload, show real upload progress, and offer a safe retry without losing the selected image.

**Architecture:** Keep the existing Base64 JSON API and backend size guard. Add small browser-side helpers in the作图 page for final size validation, XHR upload progress, and retry UI, avoiding a new resumable-upload service.

**Tech Stack:** HTML, browser JavaScript, Node.js assertion tests, Python unittest.

## Global Constraints

- Frontend encoded-image budget remains 9 MiB; backend decoded-image limit remains 10 MiB.
- Do not add chunk storage or a resumable-upload backend.
- Preserve the existing submit lock, billing error handling, and job polling behavior.
- Do not modify or deploy production servers.

---

### Task 1: Lock the upload behavior with regression tests

**Files:**
- Create: `tests/test_banana_upload_feedback.js`
- Modify: `site/workbench/banana.html`

**Interfaces:**
- Consumes: `dataUrlBytes(dataUrl)`, `refFromFile(file)`, and `submit(payload, label, endpoint)`.
- Produces: `requestJsonWithProgress(endpoint, payload, onProgress)` returning a Promise with `{s, d}`.

- [ ] **Step 1: Write failing source-contract tests**

Assert that the page performs a final `REF_MAX_BYTES` check before `setRef`, uses `XMLHttpRequest.upload.onprogress` for image payloads, renders upload percentage, and exposes a retry action that calls `submit(lastPayload, lastLabel, lastEndpoint)`.

- [ ] **Step 2: Run the test and verify RED**

Run: `node tests/test_banana_upload_feedback.js`

Expected: failure because progress transport and retry action are absent.

- [ ] **Step 3: Add minimal frontend implementation**

Add final size rejection to `refFromFile` and `revLoad`, introduce an XHR JSON helper, route image payloads through it, update the existing note with upload percentage, and keep an incompletely uploaded payload for one-click retry. When upload completed but the response is unknown, direct the user to recent works instead of risking a duplicate paid task.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `node tests/test_banana_upload_feedback.js`

Expected: all BUG-0015 assertions pass.

### Task 2: Regression verification and publication

**Files:**
- Verify: `site/workbench/banana.html`
- Verify: `tests/test_banana_upload_feedback.js`

**Interfaces:**
- Consumes: Task 1 browser-side behavior.
- Produces: an isolated BUG-0015 commit and draft PR.

- [ ] **Step 1: Run related frontend regression tests**

Run: `node tests/test_banana_upload_feedback.js && node tests/test_banana_split_workspace.js`

Expected: both commands exit 0.

- [ ] **Step 2: Run syntax/source validation**

Extract the inline script and compile it with `new Function(scriptText)`; expected exit code 0.

- [ ] **Step 3: Review scope**

Confirm only the design, plan,作图 page, and BUG-0015 test are changed.

- [ ] **Step 4: Commit and publish**

Create `codex/bug-0015-image-upload-feedback`, commit with `fix(image): improve large upload feedback`, push, and open a draft PR against `main`.
