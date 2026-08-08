# Pixelle Template Gallery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the text-only Pixelle template selector with an image gallery that exposes portrait and landscape illustration templates plus video templates.

**Architecture:** Extend the server-owned template allowlist with presentation metadata and static preview URLs. Keep the browser as a renderer/filter only, while the backend remains authoritative for valid template keys. Ship frozen preview assets with the site so gallery browsing does not call the generation service.

**Tech Stack:** Python standard library, `unittest`, static HTML/CSS/JavaScript, JPG/PNG assets.

## Global Constraints

- Start from current `origin/main` in an isolated worktree.
- Expose exactly 20 portrait illustration, 5 landscape illustration, and 2 portrait video templates.
- Do not expose asset, static, or square experimental templates.
- Preserve `1080x1920/image_default.html` as the default.
- Submit through a draft PR; do not merge or deploy.

---

### Task 1: Template catalog metadata

**Files:**
- Modify: `tests/test_pixelle_video.py`
- Modify: `server/content_domains/pixelle_video.py`

**Interfaces:**
- Produces: `public_templates() -> list[dict]` entries with `key`, `name`, `width`, `height`, `kind`, `orientation`, and `preview_url`.
- Preserves: `TEMPLATE_KEYS` as the server-side submission allowlist.

- [ ] **Step 1: Write failing catalog tests**

Add assertions that the public catalog contains 27 unique keys, the expected category counts, valid orientation metadata, website-local preview URLs, and the existing default key.

- [ ] **Step 2: Run the focused backend test and verify RED**

Run: `python -m unittest tests.test_pixelle_video.PixelleVideoTests.test_public_template_catalog_matches_deployed_allowlist -v`

Expected: FAIL because landscape/video entries and presentation metadata are absent.

- [ ] **Step 3: Implement the catalog**

Define explicit portrait illustration, landscape illustration, and portrait video name maps. Build immutable template records with the required metadata and derive `TEMPLATE_KEYS` from the full catalog.

- [ ] **Step 4: Run backend tests and verify GREEN**

Run: `python -m unittest tests.test_pixelle_video -v`

Expected: all Pixelle adapter tests pass.

### Task 2: Static template previews

**Files:**
- Create: `site/assets/pixelle-templates/1080x1920/*.{jpg,png}`
- Create: `site/assets/pixelle-templates/1920x1080/*.jpg`
- Modify: `tests/test_pixelle_video.py`

**Interfaces:**
- Consumes: each catalog entry's `preview_url`.
- Produces: one deployable image for every public template.

- [ ] **Step 1: Write a failing asset coverage test**

Resolve each catalog `preview_url` under `site/` and assert the file exists and is non-empty.

- [ ] **Step 2: Run the asset coverage test and verify RED**

Run: `python -m unittest tests.test_pixelle_video.PixelleVideoTests.test_public_template_previews_exist -v`

Expected: FAIL because the preview assets are not yet in the website tree.

- [ ] **Step 3: Copy the 27 matching Chinese preview images**

Copy only files corresponding to public template keys from Pixelle `docs/images/1080x1920` and `docs/images/1920x1080` into `site/assets/pixelle-templates/<size>/`.

- [ ] **Step 4: Re-run the asset coverage test and verify GREEN**

Run: `python -m unittest tests.test_pixelle_video.PixelleVideoTests.test_public_template_previews_exist -v`

Expected: PASS.

### Task 3: Responsive image gallery UI

**Files:**
- Modify: `tests/test_text_video_page.py`
- Modify: `site/workbench/text-video.html`

**Interfaces:**
- Consumes: template catalog metadata from `/api/gen/text-video/templates`.
- Produces: filtered template cards and dynamic preview aspect ratio.

- [ ] **Step 1: Write failing page contract tests**

Assert the page contains illustration/video tabs, portrait/landscape filters, `<img>` preview cards, image-error fallback, selected marker, and code that sets the stage aspect ratio from template orientation.

- [ ] **Step 2: Run page tests and verify RED**

Run: `python -m unittest tests.test_text_video_page -v`

Expected: FAIL because the current page renders color swatches only.

- [ ] **Step 3: Implement the gallery**

Replace swatch cards with responsive image cards, add primary and orientation filters, keep selection keyboard-accessible, and update `.tv-stage` between `9 / 16` and `16 / 9` when a template is selected.

- [ ] **Step 4: Run page tests and verify GREEN**

Run: `python -m unittest tests.test_text_video_page -v`

Expected: all text-video page tests pass.

### Task 4: Integrated verification and publication

**Files:**
- Verify all files changed in Tasks 1-3.

**Interfaces:**
- Produces: a tested branch and draft pull request against `main`.

- [ ] **Step 1: Run the focused suite**

Run: `python -m unittest tests.test_pixelle_video tests.test_text_video_page -v`

Expected: all tests pass.

- [ ] **Step 2: Inspect repository diff and asset inventory**

Run: `git status --short && git diff --check && git diff --stat && git diff -- server/content_domains/pixelle_video.py site/workbench/text-video.html tests/test_pixelle_video.py tests/test_text_video_page.py`

Expected: only scoped files and preview assets are changed; `git diff --check` exits 0.

- [ ] **Step 3: Commit and push**

Commit the scoped changes with a concise message and push `codex/pixelle-template-gallery-20260808` to `origin`.

- [ ] **Step 4: Open a draft PR**

Open a draft PR against `main` describing catalog expansion, gallery behavior, static asset strategy, and verification. Do not merge or deploy.
