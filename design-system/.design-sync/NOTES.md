# design-sync NOTES — huangque-design-system

Repo-specific gotchas for `/design-sync`. Read this before any re-sync.

## Source shape & build
- **Shape: `package`** (no Storybook). Components = the 12 PascalCase exports of `src/index.ts`; the 13th README row (`tokens`) is the styling layer, not a component.
- This repo had **no library build** originally (`dist/` was the Vite *app* demo). Two build outputs are now produced by `cfg.buildCmd` = `npm run build:ds`:
  - `npm run build:types` → `tsc -p tsconfig.build.json` emits the `.d.ts` tree to `dist/types/` (excludes `src/App.tsx` + `src/main.tsx`, the demo). `package.json` `types` points at `dist/types/index.d.ts` — this is how the converter discovers components + extracts props.
  - `npm run build:lib` → `vite build -c vite.lib.config.ts` bundles to `dist/lib/index.js` (ESM), with **react / react-dom / react/jsx-runtime external** and everything else (three, @react-three/fiber, react-reconciler, scheduler, framer-motion, gsap) **inlined**.
- The converter runs with `--entry ./dist/lib/index.js` (set as `cfg.entry`). `--node-modules ./node_modules`.
- **Why the lib build is required:** bundling the converter straight from `src/index.ts` makes esbuild hit `@react-three/fiber`'s bare `import 'scheduler'` (via react-reconciler), and the converter's react shim deliberately throws `[SCHEDULER_MISSING]` on a bare `scheduler` import → the whole IIFE crashes and all 12 exports fail. Pre-bundling with vite inlines scheduler so the converter never sees a bare import. **Do not switch the entry back to `src/`.**

## Styling model
- This is a **CSS-in-JS / inline-style DS**: all styling is inline `style={}` computed from the `tokens` object (`color/font/radius/space/shadow/gradient`), which is bundled into `_ds_bundle.js`. There is **no stylesheet**.
- `[CSS_RUNTIME]` (build + validate) is therefore **expected and non-blocking** — the converter writes a self-styling `styles.css`/`_ds_bundle.css` stub. Do NOT set `cfg.cssEntry`; there is nothing to point it at.
- It is a **dark** system (`color.bg = #070b13`). Preview cards render on a **white** background, so every authored preview wraps its story on a dark surface (a local `Stage` helper) — see below.

## Preview authoring recipe (proven across all 12)
- Author `.design-sync/previews/<Name>.tsx`; each **named PascalCase export = one zero-prop function component = one graded cell**. 2–4 per component.
- Import components AND tokens from the package name: `import { Button, color, font, radius } from 'huangque-design-system'` — it resolves to `window.HuangQue` and includes the token exports. Type children with `import type { ReactNode } from 'react'`.
- **Normal components:** wrap each story in a local `Stage` (dark `color.bg`, padding ~28, `borderRadius:16`, `font.sans`). For pill rows (Chip/Tag) give the Stage `display:flex; gap; flexWrap`.
- **Background components (ParticleField, DataFlow, WebGLParticles):** wrap in a `position:relative` + fixed height (220–320px) + `overflow:hidden` + dark container; the bg component is the absolute-fill child; overlay realistic foreground content in a `position:relative` div. Copy `WebGLParticles.tsx`.
- **GlowHero self-backgrounds** — render directly with a `height` (340–380px), no outer Stage; wrap only in `{borderRadius, overflow:hidden}`. `bg='none'` is a clean second variant. Gold-highlighted title via `title={<>…<span style={{color:color.gold}}>…</span>…</>}` (title is ReactNode).
- Use realistic 黄雀 AI-获客 / 内容工作台 content (关键词获客 / 评论区线索 / 意图过滤 / 数字人口播 / AI 作图 / 抖音·小红书·视频号 / 成交). Never filler.
- WebGL (three.js/R3F) **renders fine in this headless chromium** (SwiftShader). backdrop-filter (GlassCard) also renders.

## Known render warns (triaged, expected — not new)
- `[CSS_RUNTIME]` on build + validate — see Styling model above. Expected.
- Interactive/hover-only states render at REST in static capture: **TiltCard** tilt + gold glare are hover/cursor-only; the resting card is the correct capture (composed to look complete).

## Capture harness clock fix (IMPORTANT for re-sync)
- `package-capture.mjs` originally ran `page.clock.setFixedTime(...)`. That **freezes `performance.now()`**, which stalls framer-motion `whileInView` enter-tweens (**Reveal**) at opacity 0 → the content captures as a **blank dark box**. It hits ANY component whose visible content depends on a framer-motion enter (opacity 0→1) animation completing.
- Fix applied this run (in the staged `.ds-sync/package-capture.mjs`): commented out `setFixedTime` and added `await page.waitForTimeout(900)` after `settle()` in the per-story loop so enter animations finish. This DS has no date/time-dependent components, so real time is safe.
- **`.ds-sync/` is re-staged from the skill on every re-sync, so this patch is LOST on a fresh run.** It only matters if **Reveal** (or a new framer-motion-enter component) must be **re-captured** — and Reveal's `good` grade carries forward via the uploaded `_ds_sync.json`, so an unchanged Reveal is skipped. **If a re-sync re-captures Reveal and it comes up blank, re-apply this clock patch** (or grade Reveal from a real-time capture).

## Re-sync risks (what can silently go stale)
- **Capture clock patch is not durable** (see above). Reveal re-capture is the trigger.
- **`dist/` is gitignored** — always re-run `cfg.buildCmd` (`npm run build:ds`) before the converter on re-sync; both `dist/types/` and `dist/lib/index.js` must exist.
- **Heavy deps pinned by lockfile**: three `^0.167`, @react-three/fiber `^8.17`, framer-motion `^11`. A major bump (esp. three/R3F or React) could change render output or the scheduler-bundling assumption — re-verify the WebGL components if those move.
- Bundle is large (~2.3MB) because three.js is inlined — expected, not a regression.
- All 12 components have authored previews (none on the floor card); all graded `good`.
