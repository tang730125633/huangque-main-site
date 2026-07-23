# Audio Network Errors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace browser English network errors in AI voice generation with safe Chinese retry guidance.

**Architecture:** Add one phase-aware message mapper in the existing audio page and route submit/poll catches through it. Do not retry the paid creation POST automatically.

**Tech Stack:** HTML, browser JavaScript, Node.js assertion tests.

## Global Constraints

- Do not automatically retry `/api/gen/audio` POST requests.
- Preserve entered text, selected voice, parameters, button recovery, and business error text.
- Poll failures must not advise creating a second paid task.
- Do not modify or deploy production servers.

---

### Task 1: Add regression coverage and message mapping

**Files:**
- Create: `tests/test_audio_network_errors.js`
- Modify: `site/workbench/audio.html`

**Interfaces:**
- Consumes: browser `Error`, `navigator.onLine`, and phase `submit|poll`.
- Produces: `audioNetworkMessage(error, phase)` returning a Chinese string.

- [ ] **Step 1: Write failing tests**

Assert that the page defines the phase-aware mapper, recognizes common browser network messages, does not display `e.message` directly, gives submit retry guidance, and gives poll-specific “task may still be running” guidance.

- [ ] **Step 2: Verify RED**

Run: `node tests/test_audio_network_errors.js`

Expected: failure because the mapper and Chinese guidance are absent.

- [ ] **Step 3: Implement the minimal mapper**

Add `audioNetworkMessage(error, phase)`, use it from both catch branches, restore the button in both branches, and keep the existing request and polling flow unchanged.

- [ ] **Step 4: Verify GREEN and syntax**

Run: `node tests/test_audio_network_errors.js`

Expected: all assertions pass, including inline JavaScript compilation.

### Task 2: Verify and publish

**Files:**
- Verify: `site/workbench/audio.html`
- Verify: `tests/test_audio_network_errors.js`

**Interfaces:**
- Consumes: Task 1 message mapper.
- Produces: isolated BUG-0016 commit and draft PR.

- [ ] **Step 1: Run audio regression tests**

Run: `node tests/test_audio_network_errors.js` and the repository audio unit tests.

Expected: zero failures.

- [ ] **Step 2: Review scope**

Confirm only the audio page, BUG-0016 test, design, and plan are changed.

- [ ] **Step 3: Publish**

Create `codex/bug-0016-audio-network-errors`, commit `fix(audio): localize network failure guidance`, and open a draft PR against `main`.

