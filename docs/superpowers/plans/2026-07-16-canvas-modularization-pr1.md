# Canvas Modularization PR 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mechanically extract the existing canvas page CSS and JavaScript into cache-stamped external assets without changing canvas behavior.

**Architecture:** `canvas.html` remains the DOM shell. Its current inline stylesheet moves verbatim to `site/workbench/canvas/canvas.css`, and its final inline application IIFE moves verbatim to `site/workbench/canvas/canvas-app.js`. The existing `scripts/stamp_assets.py` registry owns both new page-scoped cache stamps so later edits cannot ship with stale browser assets.

**Tech Stack:** Static HTML/CSS, browser JavaScript in the existing IIFE style, Python 3.12 `unittest`, Node.js 22 syntax checks, existing cache-stamp tooling.

## Global Constraints

- This plan covers PR 1 only; do not extract state, graph, runner, collaboration, renderer, interaction, API, storage, or export modules yet.
- Preserve all existing DOM IDs, visible copy, script order, localStorage keys, API paths, canvas data formats, and collaboration protocol.
- The extracted CSS and JavaScript payloads must be byte-equivalent after newline normalization to the current inline payloads identified by the SHA-256 values in Task 1.
- Do not introduce React, Vue, React Flow, a bundler, npm dependencies, or a build step.
- Keep `canvas-collab-sync.js` as a separate script loaded before `canvas/canvas-app.js`.
- All work stays in the `E-canvas` conflict group and the `codex/canvas-modularization` branch.
- Use selective `git add` paths; never use `git add -A`.
- Do not push, create a PR, merge, or deploy until the user explicitly approves that action.

## File Map

- Create `site/workbench/canvas/canvas.css`: verbatim contents of the current `canvas.html` `<style>` block.
- Create `site/workbench/canvas/canvas-app.js`: verbatim contents of the current final inline `<script>` block.
- Modify `site/workbench/canvas.html`: replace the extracted inline blocks with external cache-stamped references.
- Modify `scripts/stamp_assets.py`: register both page-scoped assets as optional hash-stamped resources.
- Modify `tests/test_stamp_assets.py`: require both canvas assets in the stamp registry and verify their current stamps in `canvas.html`.
- Create `tests/test_canvas_asset_extraction.py`: guard verbatim extraction, external references, and script order.

---

### Task 1: Lock the extraction contract and move the inline assets

**Files:**
- Create: `tests/test_canvas_asset_extraction.py`
- Create: `site/workbench/canvas/canvas.css`
- Create: `site/workbench/canvas/canvas-app.js`
- Modify: `site/workbench/canvas.html:11-312`
- Modify: `site/workbench/canvas.html:478-3879`

**Interfaces:**
- Consumes: existing DOM in `canvas.html`; existing browser global `window.HQCanvasCollabSync` from `canvas-collab-sync.js`.
- Produces: `canvas/canvas.css` stylesheet and `canvas/canvas-app.js` application script, both referenced by `canvas.html`.

- [ ] **Step 1: Add a failing extraction contract test**

Create `tests/test_canvas_asset_extraction.py` with exactly this content:

```python
import hashlib
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = ROOT / "site" / "workbench" / "canvas.html"
CSS_PATH = ROOT / "site" / "workbench" / "canvas" / "canvas.css"
APP_PATH = ROOT / "site" / "workbench" / "canvas" / "canvas-app.js"

EXPECTED_CSS_SHA256 = "96c2cf4a29c2fcd04113c920f198783f07a2794d3a6959582986b46a95353396"
EXPECTED_APP_SHA256 = "4e864ecf3e7045d0fa9f64d212b35f55e41838bd2d0fb03e2f4b5e71883c2238"


def normalized_sha256(path: Path) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").rstrip("\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CanvasAssetExtractionTests(unittest.TestCase):
    def test_inline_payloads_are_external_and_unchanged(self):
        self.assertTrue(CSS_PATH.is_file(), CSS_PATH)
        self.assertTrue(APP_PATH.is_file(), APP_PATH)
        self.assertEqual(EXPECTED_CSS_SHA256, normalized_sha256(CSS_PATH))
        self.assertEqual(EXPECTED_APP_SHA256, normalized_sha256(APP_PATH))

        html = HTML_PATH.read_text(encoding="utf-8")
        self.assertNotRegex(html, r"(?s)<style>.*?</style>")
        self.assertNotRegex(html, r"(?s)<script>\s*/\* 节点生产画布")

    def test_canvas_assets_are_versioned_and_loaded_in_order(self):
        html = HTML_PATH.read_text(encoding="utf-8")
        css = re.search(r'href="canvas/canvas\.css\?v=([0-9a-f]{8})"', html)
        app = re.search(r'src="canvas/canvas-app\.js\?v=([0-9a-f]{8})"', html)
        self.assertIsNotNone(css, "canvas stylesheet must have a content stamp")
        self.assertIsNotNone(app, "canvas application script must have a content stamp")
        self.assertLess(html.index("cloud-shell.js?v="), html.index("canvas-collab-sync.js?v="))
        self.assertLess(html.index("canvas-collab-sync.js?v="), html.index("canvas/canvas-app.js?v="))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and verify the expected failure**

Run:

```powershell
python -m unittest tests.test_canvas_asset_extraction -v
```

Expected: FAIL because `site/workbench/canvas/canvas.css` and `canvas-app.js` do not exist and `canvas.html` still contains inline blocks.

- [ ] **Step 3: Mechanically extract the current blocks**

Perform one newline-preserving mechanical rewrite:

1. Capture the content between the only `<style>` and `</style>` tags, excluding the tags.
2. Write that payload to `site/workbench/canvas/canvas.css` as UTF-8 without BOM.
3. Capture the content between the final inline `<script>` and `</script>` tags at lines 478–3879, excluding the tags.
4. Write that payload to `site/workbench/canvas/canvas-app.js` as UTF-8 without BOM.
5. Replace the style block with:

```html
<link rel="stylesheet" href="canvas/canvas.css?v=00000000">
```

6. Replace the final inline script block with:

```html
<script src="canvas/canvas-app.js?v=00000000"></script>
```

Do not reindent, rename, format, or otherwise edit either extracted payload. The temporary `00000000` stamps are replaced by Task 2.

- [ ] **Step 4: Confirm the extraction hashes before continuing**

Run:

```powershell
python -m unittest tests.test_canvas_asset_extraction.CanvasAssetExtractionTests.test_inline_payloads_are_external_and_unchanged -v
node --check site/workbench/canvas/canvas-app.js
```

Expected: both commands PASS. If a hash differs, restore only these three canvas files from the task start and repeat the mechanical extraction; do not update the expected hashes.

- [ ] **Step 5: Commit the mechanical extraction**

Run:

```powershell
git add tests/test_canvas_asset_extraction.py site/workbench/canvas.html site/workbench/canvas/canvas.css site/workbench/canvas/canvas-app.js
git diff --cached --check
git commit -m "refactor: extract canvas page assets"
```

Expected: one commit containing only the four listed paths. The full extraction test may still fail on the temporary cache stamp until Task 2; the payload-preservation test and JavaScript syntax check must pass.

---

### Task 2: Add content-hash cache stamps for the page-scoped assets

**Files:**
- Modify: `scripts/stamp_assets.py:51-55`
- Modify: `tests/test_stamp_assets.py:34-48`
- Modify: `site/workbench/canvas.html`

**Interfaces:**
- Consumes: `Asset(name, required)` and the existing HTML rewrite loop in `scripts/stamp_assets.py`.
- Produces: optional assets named `canvas/canvas.css` and `canvas/canvas-app.js`, each with an independent eight-character MD5 content stamp.

- [ ] **Step 1: Add failing registry and real-reference tests**

Add these methods to `AssetRegistryTests` in `tests/test_stamp_assets.py`:

```python
    def test_canvas_assets_are_registered_as_optional(self):
        assets = {a.name: a for a in stamp_assets.ASSETS}
        self.assertIn("canvas/canvas.css", assets)
        self.assertIn("canvas/canvas-app.js", assets)
        self.assertFalse(assets["canvas/canvas.css"].required)
        self.assertFalse(assets["canvas/canvas-app.js"].required)

    def test_canvas_html_uses_current_canvas_asset_stamps(self):
        html = (ROOT / "site" / "workbench" / "canvas.html").read_bytes()
        assets = {a.name: a for a in stamp_assets.ASSETS}
        for name in ("canvas/canvas.css", "canvas/canvas-app.js"):
            asset = assets[name]
            match = asset.pattern.search(html)
            self.assertIsNotNone(match, name)
            self.assertEqual(asset.stamp().encode("ascii"), match.group(2), name)
```

- [ ] **Step 2: Run the focused tests and verify the expected failure**

Run:

```powershell
python -m unittest tests.test_stamp_assets.AssetRegistryTests.test_canvas_assets_are_registered_as_optional -v
```

Expected: FAIL because neither canvas asset is present in `ASSETS`.

- [ ] **Step 3: Register both assets without making them globally required**

Change `ASSETS` in `scripts/stamp_assets.py` to:

```python
ASSETS = (
    Asset("cloud-shell.js", required=True),
    Asset("theme.css", required=False),
    Asset("theme-init.js", required=False),
    Asset("canvas/canvas.css", required=False),
    Asset("canvas/canvas-app.js", required=False),
)
```

Optional is required here: only `canvas.html` references these page-scoped assets, so other workbench pages must not fail with “missing required stamp.”

- [ ] **Step 4: Generate the real stamps and run the focused tests**

Run:

```powershell
python scripts/stamp_assets.py
python -m unittest tests.test_stamp_assets -v
python -m unittest tests.test_canvas_asset_extraction -v
python scripts/stamp_assets.py --check
```

Expected: all tests PASS, and `canvas.html` contains the current independent hashes instead of `00000000`.

- [ ] **Step 5: Commit the cache-stamp integration**

Run:

```powershell
git add scripts/stamp_assets.py tests/test_stamp_assets.py site/workbench/canvas.html
git diff --cached --check
git commit -m "test: guard canvas asset cache stamps"
```

Expected: one commit containing only the three listed paths.

---

### Task 3: Run canvas regression and repository quality gates

**Files:**
- Modify: `tests/test_canvas_realtime_sync.js`
- Modify: `tests/test_canvas_board_card_layout.js`
- Verify only; do not modify files unless a gate identifies a concrete extraction regression.

**Interfaces:**
- Consumes: the externally loaded CSS and application script from Tasks 1–2.
- Produces: evidence that the mechanical move did not change syntax, collaboration logic, board layout, cache stamps, or repository safety checks.

- [ ] **Step 1: Run canvas-specific tests and syntax checks**

Run:

```powershell
node --check site/workbench/canvas/canvas-app.js
node --check site/workbench/canvas-collab-sync.js
node tests/test_canvas_realtime_sync.js
node tests/test_canvas_board_card_layout.js
python -m unittest tests.test_canvas_asset_extraction -v
python -m unittest tests.test_stamp_assets -v
python -m unittest tests.test_auth_canvas_collab -v
```

Expected: every command exits 0 with no failures.

- [ ] **Step 2: Run the local base gates**

Run:

```powershell
python scripts/ci_validate.py
python -m compileall -q server scripts worker
python scripts/stamp_assets.py --check
git diff --check origin/main...HEAD
```

Expected: every command exits 0. `stamp_assets.py --check` prints `cache stamps OK`.

- [ ] **Step 3: Run the full Python test suite**

Run:

```powershell
python -m unittest discover -s tests -v
```

Expected: PASS with zero failures. If Windows-only SQLite locking, encoding, or missing Bash causes a failure, record the exact test and output for GitHub CI; do not weaken or skip the test in repository configuration.

- [ ] **Step 4: Inspect the complete PR 1 difference**

Run:

```powershell
git diff --check origin/main...HEAD
git diff --stat origin/main...HEAD
git diff --name-only origin/main...HEAD
git status --short
```

Expected paths:

```text
docs/superpowers/plans/2026-07-16-canvas-modularization-pr1.md
docs/superpowers/specs/2026-07-16-canvas-modularization-design.md
scripts/stamp_assets.py
site/workbench/canvas.html
site/workbench/canvas/canvas-app.js
site/workbench/canvas/canvas.css
tests/test_canvas_asset_extraction.py
tests/test_canvas_realtime_sync.js
tests/test_canvas_board_card_layout.js
tests/test_stamp_assets.py
```

No database, generated output, credentials, temporary files, unrelated HTML, or server code may appear.

---

### Task 4: Rebase, re-verify, and prepare the Ready PR handoff

**Files:**
- Verify only; any conflict resolution must remain within the approved PR 1 paths.

**Interfaces:**
- Consumes: completed PR 1 commits and latest `origin/main`.
- Produces: a clean, rebased branch ready for an explicitly authorized push and Ready PR.

- [ ] **Step 1: Fetch and rebase the latest main**

Run:

```powershell
git fetch origin --prune
git rebase origin/main
```

Expected: successful rebase. If conflicts occur, resolve only approved PR 1 files, stage them selectively, run `git rebase --continue`, and repeat Task 3 in full.

- [ ] **Step 2: Repeat the final mandatory checks**

Run:

```powershell
python scripts/stamp_assets.py --check
python scripts/ci_validate.py
python -m compileall -q server scripts worker
node --check site/workbench/canvas/canvas-app.js
node tests/test_canvas_realtime_sync.js
node tests/test_canvas_board_card_layout.js
python -m unittest tests.test_canvas_asset_extraction tests.test_stamp_assets tests.test_auth_canvas_collab -v
git diff --check origin/main...HEAD
git status --short
```

Expected: every check exits 0 and the working tree is clean.

- [ ] **Step 3: Stop for explicit publication approval**

Report the branch, commit list, changed files, test results, and remaining Windows-only limitations. Do not run `git push`, `gh pr create`, merge, or deployment commands until the user explicitly authorizes publication.
