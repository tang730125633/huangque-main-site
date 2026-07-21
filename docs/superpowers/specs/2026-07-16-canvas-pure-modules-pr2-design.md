# Canvas Pure Modules PR 2 Design

## Purpose

PR 2 reduces `site/workbench/canvas/canvas-app.js` by extracting logic that can be understood and tested without owning page rendering or user interaction. It introduces five modules—state, graph, storage, API, and export—while preserving the current UI, DOM IDs, API contracts, local data, and collaboration behavior.

This PR is structural only. It does not add features, redesign the canvas, change server code, or introduce a build step.

## Scope

Create these browser/Node-compatible modules under `site/workbench/canvas/`:

- `canvas-state.js`
- `canvas-graph.js`
- `canvas-storage.js`
- `canvas-api.js`
- `canvas-export.js`

Each module uses the repository's existing UMD pattern: CommonJS exports for Node tests and one property under `window.HQCanvas` in the browser. `canvas-app.js` remains the page entry point and consumes the new modules through explicit interfaces.

The modules load before `canvas-app.js`, receive independent content cache stamps, and do not create new top-level browser globals outside `window.HQCanvas`.

## Non-goals

- Do not change the visual design, copy, keyboard shortcuts, menus, dialogs, or node types.
- Do not change DOM IDs, API paths, request/response formats, localStorage keys, canvas snapshot formats, or collaboration protocols.
- Do not extract rendering, pointer/keyboard interactions, task scheduling, or collaboration orchestration; those remain PR 3 work.
- Do not introduce React, Vue, ES module loading, npm dependencies, bundlers, or transpilation.
- Do not fix unrelated product bugs found during extraction. Lock them with a regression test and handle the behavior change separately.

## Architecture and dependency direction

```text
canvas.html
  -> canvas-state.js
  -> canvas-graph.js
  -> canvas-storage.js
  -> canvas-api.js
  -> canvas-export.js
  -> canvas-collab-sync.js
  -> canvas-app.js

canvas-app
  -> state + graph + storage + api + export + collab-sync
```

The five new modules do not depend on `canvas-app.js` or on each other's private state. Where one concern needs another—for example, exporting a snapshot or saving it—`canvas-app.js` passes the snapshot explicitly. This prevents circular dependencies and keeps the application entry point responsible for orchestration.

## Module contracts

### `canvas-state.js`

This module owns serializable snapshot cloning and the undo/redo history. Its public contract is:

```text
cloneSnapshot(snapshot)
createHistory({ limit })
history.push(snapshot)
history.undo(currentSnapshot) -> snapshot | null
history.redo(currentSnapshot) -> snapshot | null
history.clear()
history.canUndo()
history.canRedo()
```

The live node registry currently contains DOM references, so PR 2 does not move renderer-owned node objects into this module. `canvas-app.js` remains responsible for capturing and restoring live UI state, but the history stacks and all values crossing this boundary are plain serializable snapshots. PR 3 can move the remaining live registry after renderer and interaction boundaries exist, without introducing a second source of truth in PR 2.

### `canvas-graph.js`

This module contains pure graph and geometry functions:

```text
detectCycle(nodes, edges) -> nodeIds
topologicalOrder(nodes, edges) -> nodeIds
computeAutoLayout(nodes, edges, options) -> positionsById
contentBounds(nodes, options) -> rectangle
```

It never reads or writes DOM elements. `canvas-app.js` converts live nodes into plain inputs, calls the module, then applies returned positions to the existing UI. Cycle order and current layout coordinates must remain behavior-compatible.

### `canvas-storage.js`

This module centralizes access to the existing storage keys:

- `hq_canvas_draft_v2`
- `hq_canvas_templates_v2`
- `hq_canvas_boards_v1`
- `hq_canvas_active_id`

It accepts a storage implementation so Node tests can use an in-memory fake. It exposes draft, board, template, and active-board read/write/remove operations plus existing normalization and heavy-output cleanup helpers. Results distinguish unavailable storage, corrupt JSON, and quota exhaustion through structured error codes. User-facing messages remain the responsibility of `canvas-app.js`.

Image compression keeps the current browser implementation and is injected into storage cleanup where needed; the storage module itself does not create DOM elements.

### `canvas-api.js`

This module wraps the existing `fetch` paths behind an injected `fetchImpl` and token provider. It owns:

- cookie-authenticated JSON requests
- JSON body/header construction
- request timeouts and cancellation
- HTTP and malformed-response normalization
- authenticated asset fetching

It returns data or structured errors containing `status`, `code`, and a safe message. It does not call dialogs, mutate nodes, update run state, or touch the DOM. Existing endpoint paths and request payloads remain unchanged.

### `canvas-export.js`

This module owns template JSON serialization/parsing and canvas JPG generation. Its pure helpers validate template data, normalize filenames, calculate wrapped text, and compute export geometry. Browser-only rendering accepts explicit dependencies for image loading, canvas creation, and download, allowing its decision logic to be tested in Node.

The output filename pattern, JPG quality, theme colors, node content, and template JSON format remain unchanged.

## Migration sequence

PR 2 remains one reviewable PR with five ordered implementation commits:

1. Extract graph algorithms and geometry.
2. Extract snapshot cloning and undo/redo history.
3. Extract local storage and compatibility handling.
4. Extract request and error normalization.
5. Extract template/JPG export logic and finalize script loading/cache stamps.

For every module, the sequence is test first, module implementation second, application integration third, and old implementation removal last. The application must never retain two active implementations after a migration commit.

## Error handling

- Graph functions reject malformed inputs without mutating caller data.
- History ignores null snapshots and enforces the existing 60-entry limit.
- Storage returns explicit error codes for corrupt data, unavailable storage, and quota exhaustion; it must not silently overwrite valid data after a parse failure.
- API errors preserve HTTP status and use safe messages. Abort and timeout are distinct error codes.
- Export rejects invalid template files and failed image loads without leaving a download action or a permanently busy UI state.
- `canvas-app.js` remains the only layer that chooses dialogs, node notes, and save-state text.

## Compatibility guarantees

- Existing snapshots load without migration.
- Undo/redo order and the 60-entry cap remain unchanged.
- Cycle detection returns the same affected node set.
- Auto-layout produces the same coordinates for existing graphs.
- Existing draft, board, template, and active-board values remain readable and writable under the same keys.
- Existing generation, asset, and collaboration HTTP traffic retains its path, method, headers, and JSON payload.
- Exported templates and JPG filenames retain their current formats.

## Testing strategy

Add one Node test file per module and keep the existing canvas regressions:

- `tests/test_canvas_state.js`: snapshot isolation, history cap, undo/redo, clear.
- `tests/test_canvas_graph.js`: cycle/no-cycle, topology, layout, bounds, input immutability.
- `tests/test_canvas_storage.js`: legacy/invalid values, key compatibility, quota and unavailable storage.
- `tests/test_canvas_api.js`: JSON requests, HTTP errors, malformed JSON, timeout, abort, asset auth.
- `tests/test_canvas_export.js`: template round-trip, invalid input, filename, wrapping/geometry, failed image load.
- Existing `test_canvas_realtime_sync.js`, `test_canvas_board_card_layout.js`, extraction/stamp tests, and `test_auth_canvas_collab.py` remain mandatory.

The cache-stamp registry and HTML loading-order tests expand to cover all five scripts. GitHub Actions remains the authoritative Linux full-suite gate; local targeted tests, syntax checks, static validation, cache-stamp checks, and diff checks must pass before publication.

## Acceptance criteria

- All five modules exist, are independently testable in Node, and load before `canvas-app.js`.
- No migrated implementation remains duplicated in `canvas-app.js`.
- No new JavaScript file exceeds 800 lines unless the design is re-reviewed.
- The five new modules do not access application-private globals or directly display UI.
- Current local boards, templates, collaboration boards, graph behavior, undo/redo, requests, and exports remain compatible.
- PR 2 contains only module extraction, tests, cache-stamp/loading changes, and its design/plan documentation.
- Required local gates and GitHub Actions pass before merge.
